#!/usr/bin/env python3
"""
DZO / Prozorro Monitor — стартова версія під Prozorro API.

Мета: щодня ловити НОВІ закупівлі на ПРОЕКТНІ РОБОТИ, що:
  • мають статус «період уточнень» (active.enquiries)
    або «подання пропозицій» (active.tendering);
  • на суму від 300 000 грн (UAH);
  • у регіонах: м. Київ, Київська область, Житомирська область.
Знайдене — короткий дайджест у Telegram + запис у стрічку для сайту.

────────────────────────────────────────────────────────────────────────
Звірено з живим API (public-api.prozorro.gov.ua/api/2.5):
  • Фід /tenders повертає без авторизації, але лише {id, dateModified}
    + те, що явно попросити через opt_fields. Повноцінного пошукового
    API з серверною фільтрацією за статусом/регіоном/CPV/сумою у
    Prozorro немає — є лише opt_fields=status,procuringEntity (value,
    items, tenderPeriod через opt_fields НЕ повертаються).
  • Тож фід просять з opt_fields=status,procuringEntity — це дозволяє
    відсіяти за статусом і регіоном ще на етапі списку, а повні деталі
    (для перевірки CPV і суми) тягнути лише для тих, що пройшли
    попередній фільтр. Це і є «серверна фільтрація» в межах того, що
    реально підтримує API.
  • Назви полів status / value.amount / value.currency /
    procuringEntity.address.region / items[].classification.id —
    підтверджені на реальних тендерах, без змін.
────────────────────────────────────────────────────────────────────────
"""

import os
import json
import html
import time
import datetime as dt
from pathlib import Path

import requests

# ── Конфіг (env / GitHub Secrets) ────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")  # необов'язково

# Офіційний відкритий фід Prozorro (читання без токена).
# TODO[claude-code]: перевірити доступність саме цього хоста; за потреби
# замінити на пошуковий API чи інший дзеркальний ендпоінт.
PROZORRO_FEED = os.environ.get(
    "PROZORRO_FEED", "https://public-api.prozorro.gov.ua/api/2.5/tenders"
)

# ── Критерії відбору ─────────────────────────────────────────────────
TARGET_STATUSES = {"active.enquiries", "active.tendering"}
CPV_PREFIXES    = ("71",)        # проектні / архітектурно-інженерні (ДК021)
MIN_AMOUNT      = 300_000.0      # грн
# Регіони пишемо у кількох варіантах написання — Prozorro не завжди однаковий
TARGET_REGIONS  = {
    "м. київ", "місто київ", "м.київ", "київ",
    "київська область", "київська обл", "київська обл.",
    "житомирська область", "житомирська обл", "житомирська обл.",
}

# Запобіжники, щоб денний прогін не перетворився на тисячі запитів
MAX_PAGES         = int(os.environ.get("MAX_PAGES", "60"))      # сторінок фіда за прогін
MAX_DETAIL_FETCH  = int(os.environ.get("MAX_DETAIL_FETCH", "1500"))
REQUEST_PAUSE     = 0.15         # пауза між запитами деталей, сек

DATA_DIR   = Path(__file__).parent / "data"
SEEN_PATH  = DATA_DIR / "seen.json"
FEED_PATH  = DATA_DIR / "dzo_feed.json"
STATE_PATH = DATA_DIR / "state.json"   # зберігаємо offset фіда між прогонами
FEED_LIMIT = 50

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "ArmabudProzorroBot/1.0"})

RETRY_DELAYS = [5, 15, 45]  # секунди між повторними спробами


def api_get(url, params=None, timeout=40):
    """GET з автоматичним retry (3 спроби) та обробкою 429."""
    for attempt, delay in enumerate([0] + RETRY_DELAYS, 1):
        if delay:
            print(f"[retry] спроба {attempt}, чекаємо {delay}с…")
            time.sleep(delay)
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", delay or 30))
                print(f"[warn] 429 Too Many Requests, чекаємо {retry_after}с…")
                time.sleep(retry_after)
                continue
            r.raise_for_status()
            return r
        except requests.exceptions.Timeout:
            print(f"[warn] timeout на {url}")
            if attempt > len(RETRY_DELAYS):
                raise
        except requests.exceptions.ConnectionError as e:
            print(f"[warn] connection error: {e}")
            if attempt > len(RETRY_DELAYS):
                raise
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code in (500, 502, 503, 504):
                print(f"[warn] server error {code}")
                if attempt > len(RETRY_DELAYS):
                    raise
            else:
                raise  # 4xx (крім 429) — не повторюємо
    raise RuntimeError(f"api_get failed after retries: {url}")


# ── Стан фіда (offset) ───────────────────────────────────────────────
def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"offset": None}


def save_state(state):
    DATA_DIR.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _region_ok(pe):
    region = ((pe.get("address") or {}).get("region") or "").strip().lower()
    return region in TARGET_REGIONS


# ── Збір id-кандидатів з фіда (попередній фільтр статус+регіон) ───────
def fetch_candidate_ids(offset):
    """Гортає фід від збереженого offset; повертає (ids, new_offset).

    opt_fields=status,procuringEntity дозволяє відсіяти тендери за
    статусом і регіоном замовника прямо тут, без запиту повних деталей
    кожного тендера (value/items опт_fields не повертає — для CPV і
    суми все одно треба буде fetch_tender нижче).
    """
    ids, pages = [], 0
    url = PROZORRO_FEED
    base_params = {"limit": 100, "descending": "0", "opt_fields": "status,procuringEntity"}
    params = dict(base_params)
    if offset:
        params["offset"] = offset

    while pages < MAX_PAGES:
        try:
            r = api_get(url, params=params)
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code in (400, 404) and offset:
                print(f"[warn] offset недійсний ({code}), скидаємо і починаємо спочатку.")
                params = dict(base_params)
                offset = None
                continue
            print(f"[error] fetch_candidate_ids: {e}")
            break
        except Exception as e:
            print(f"[error] fetch_candidate_ids: {e}")
            break
        body = r.json()
        data = body.get("data", [])
        if not data:
            break
        for t in data:
            tid = t.get("id")
            if not tid:
                continue
            if t.get("status") not in TARGET_STATUSES:
                continue
            if not _region_ok(t.get("procuringEntity", {}) or {}):
                continue
            ids.append(tid)
        # наступна сторінка
        nxt = body.get("next_page", {})
        new_offset = nxt.get("offset")
        if not new_offset or new_offset == offset:
            offset = new_offset
            break
        offset = new_offset
        params = {**base_params, "offset": offset}
        pages += 1

    return ids, offset


# ── Деталі одного тендера ────────────────────────────────────────────
def fetch_tender(tid):
    r = api_get(f"{PROZORRO_FEED}/{tid}")
    return r.json().get("data", {})


# ── Перевірка під критерії ───────────────────────────────────────────
def matches(t):
    # 1) статус
    if t.get("status") not in TARGET_STATUSES:
        return False

    # 2) сума
    val = t.get("value", {}) or {}
    amount = float(val.get("amount") or 0)
    currency = (val.get("currency") or "").upper()
    if currency and currency != "UAH":
        return False
    if amount < MIN_AMOUNT:
        return False

    # 3) проектні роботи за CPV (ДК021) в items
    cpv_ok = False
    for it in t.get("items", []) or []:
        cid = ((it.get("classification") or {}).get("id") or "")
        if cid.startswith(CPV_PREFIXES):
            cpv_ok = True
            break
    if not cpv_ok:
        return False

    # 4) регіон замовника (з кількома варіантами доставки/замовника)
    region = ""
    pe = t.get("procuringEntity", {}) or {}
    region = ((pe.get("address") or {}).get("region") or "").strip().lower()
    if region not in TARGET_REGIONS:
        # запасний варіант — регіон доставки в items
        for it in t.get("items", []) or []:
            dr = ((it.get("deliveryAddress") or {}).get("region") or "").strip().lower()
            if dr in TARGET_REGIONS:
                region = dr
                break
    if region not in TARGET_REGIONS:
        return False

    return True


def to_record(t):
    val = t.get("value", {}) or {}
    pe = t.get("procuringEntity", {}) or {}
    tid = t.get("id", "")
    status_ua = {"active.enquiries": "Період уточнень",
                 "active.tendering": "Подання пропозицій"}.get(t.get("status"), t.get("status"))
    end = ((t.get("tenderPeriod") or {}).get("endDate") or "")[:16].replace("T", " ")
    return {
        "title": t.get("title", "Без назви"),
        "summary": f"{pe.get('name','')} · {status_ua}",
        "amount": f"{float(val.get('amount') or 0):,.0f} грн".replace(",", " "),
        "deadline": end,
        "region": ((pe.get("address") or {}).get("region") or ""),
        # посилання на картку в Prozorro (можна замінити на DZO-вьюер)
        "url": f"https://prozorro.gov.ua/tender/{tid}",
    }


# ── Стан «вже бачили» ────────────────────────────────────────────────
def load_seen():
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
    return set()


def save_seen(seen):
    DATA_DIR.mkdir(exist_ok=True)
    SEEN_PATH.write_text(json.dumps(sorted(seen)[-5000:], ensure_ascii=False),
                         encoding="utf-8")


# ── Telegram ─────────────────────────────────────────────────────────
def send_telegram(records):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("[warn] немає TELEGRAM_BOT_TOKEN/CHAT_ID — пропускаю надсилання.")
        return
    today = dt.date.today().strftime("%d.%m.%Y")
    lines = [f"<b>🏗 Нові тендери (проектні роботи) — {today}</b>", ""]
    for r in records:
        title = html.escape(r["title"])
        lines.append(
            f"• <a href=\"{html.escape(r['url'])}\"><b>{title}</b></a>\n"
            f"  {html.escape(r['summary'])}\n"
            f"  💰 {html.escape(r['amount'])}   📍 {html.escape(r['region'])}\n"
            f"  ⏳ до {html.escape(r['deadline'])}"
        )
    text = "\n\n".join(lines)[:4000]
    SESSION.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    ).raise_for_status()
    print(f"[ok] надіслано в Telegram: {len(records)} тендер(ів).")


# ── Стрічка для сайту ────────────────────────────────────────────────
def update_feed(records):
    DATA_DIR.mkdir(exist_ok=True)
    old = []
    if FEED_PATH.exists():
        old = json.loads(FEED_PATH.read_text(encoding="utf-8")).get("items", [])
    now = dt.datetime.now().isoformat(timespec="minutes")
    items = ([{**r, "added": now} for r in records] + old)[:FEED_LIMIT]
    FEED_PATH.write_text(
        json.dumps({"updated": now, "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"[ok] оновлено стрічку сайту: {FEED_PATH}")


# ── Головний потік ───────────────────────────────────────────────────
def should_reset_monthly(state):
    """Повертає True якщо з останнього скидання минув місяць."""
    last = state.get("last_reset")
    if not last:
        return True
    return dt.date.today() >= dt.date.fromisoformat(last) + dt.timedelta(days=30)


def main():
    state = load_state()
    seen = set()

    try:
        # ── Щомісячне скидання ────────────────────────────────────────
        if should_reset_monthly(state):
            print("[info] місячне скидання seen.json та offset — починаємо спочатку.")
            state["offset"] = None
            state["last_reset"] = dt.date.today().isoformat()
        else:
            seen = load_seen()

        ids, new_offset = fetch_candidate_ids(state.get("offset"))
        print(f"[info] кандидатів з фіда (статус+регіон пройшли): {len(ids)}.")

        fresh_ids = [i for i in ids if i not in seen][:MAX_DETAIL_FETCH]
        print(f"[info] нових для перевірки: {len(fresh_ids)} (ліміт {MAX_DETAIL_FETCH}).")

        matched = []
        for i, tid in enumerate(fresh_ids, 1):
            try:
                t = fetch_tender(tid)
                if matches(t):
                    matched.append(to_record(t))
            except Exception as e:
                print(f"[warn] tender {tid}: {e}")
            seen.add(tid)
            if i % 200 == 0:
                print(f"[info] оброблено {i}/{len(fresh_ids)}…")
            time.sleep(REQUEST_PAUSE)

        print(f"[info] під критерії підійшло: {len(matched)}.")

        if matched:
            try:
                send_telegram(matched)
            except Exception as e:
                print(f"[warn] Telegram: {e}")
            update_feed(matched)

        if new_offset:
            state["offset"] = new_offset

    except Exception as e:
        print(f"[error] несподівана помилка: {e}")
        # скидаємо offset щоб наступний запуск почав спочатку
        state["offset"] = None

    finally:
        # зберігаємо стан і seen завжди — навіть при помилці
        save_seen(seen)
        save_state(state)
        print("[done]")


if __name__ == "__main__":
    main()

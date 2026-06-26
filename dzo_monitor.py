#!/usr/bin/env python3
"""
DZO Monitor — щоденний агент моніторингу dzo.com.ua.

Алгоритм:
  1. Зібрати поточні матеріали (Tavily, якщо є ключ; інакше — прямий парсинг сторінки).
  2. Відкинути ті, що вже бачили (data/seen.json).
  3. Claude фільтрує нове під критерії АрмаБуд і робить короткий дайджест.
  4. Надіслати дайджест у Telegram.
  5. Оновити data/seen.json і data/dzo_feed.json (стрічку для сайту).

Запускається GitHub Actions раз на день (див. .github/workflows/dzo-monitor.yml).
Запуск вручну:  python dzo_monitor.py
"""

import os
import json
import html
import datetime as dt
from pathlib import Path

import requests

# ── Конфіг (усе з env / GitHub Secrets) ──────────────────────────────
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
TAVILY_API_KEY     = os.environ.get("TAVILY_API_KEY", "")  # необов'язково
DZO_URL            = os.environ.get("DZO_URL", "https://dzo.com.ua")

# Під які критерії фільтрувати (можна змінювати без правки коду — через env)
FILTER_CRITERIA = os.environ.get(
    "DZO_FILTER",
    "відбудова та капітальний ремонт, проектні роботи, тендери у будівництві, "
    "об'єкти у Київській області та м. Києві, реконструкція пошкоджених будівель"
)

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
DATA_DIR   = Path(__file__).parent / "data"
SEEN_PATH  = DATA_DIR / "seen.json"
FEED_PATH  = DATA_DIR / "dzo_feed.json"
FEED_LIMIT = 50  # скільки останніх записів тримати у стрічці для сайту


# ── Збір матеріалів ──────────────────────────────────────────────────
def collect_items():
    """Повертає список dict: {title, url, snippet}."""
    if TAVILY_API_KEY:
        try:
            return _collect_via_tavily()
        except Exception as e:
            print(f"[warn] Tavily не спрацював ({e}); переходжу на прямий парсинг.")
    return _collect_via_scrape()


def _collect_via_tavily():
    r = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": TAVILY_API_KEY,
            "query": f"нові тендери та новини будівництва відбудова site:dzo.com.ua",
            "search_depth": "advanced",
            "max_results": 20,
            "include_domains": ["dzo.com.ua"],
        },
        timeout=40,
    )
    r.raise_for_status()
    out = []
    for it in r.json().get("results", []):
        out.append({
            "title": (it.get("title") or "").strip(),
            "url": it.get("url", "").strip(),
            "snippet": (it.get("content") or "")[:500].strip(),
        })
    return out


def _collect_via_scrape():
    from bs4 import BeautifulSoup
    r = requests.get(DZO_URL, timeout=40, headers={"User-Agent": "ArmabudDZOBot/1.0"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen_urls = [], set()
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = a["href"]
        if len(text) < 15:           # пропускаємо меню/короткі лінки
            continue
        if href.startswith("/"):
            href = DZO_URL.rstrip("/") + href
        if not href.startswith("http") or href in seen_urls:
            continue
        seen_urls.add(href)
        out.append({"title": text, "url": href, "snippet": ""})
    return out[:40]


# ── Стан (що вже бачили) ─────────────────────────────────────────────
def load_seen():
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
    return set()


def save_seen(seen):
    DATA_DIR.mkdir(exist_ok=True)
    # тримаємо останні 1000 id, щоб файл не ріс безкінечно
    SEEN_PATH.write_text(json.dumps(sorted(seen)[-1000:], ensure_ascii=False, indent=0),
                         encoding="utf-8")


# ── Claude: фільтр + дайджест ────────────────────────────────────────
def summarize_with_claude(items):
    """Повертає список релевантних: {title, summary, deadline, url}."""
    if not ANTHROPIC_API_KEY:
        # без ключа — віддаємо як є, без фільтрації
        return [{"title": i["title"], "summary": i["snippet"][:200],
                 "deadline": "", "url": i["url"]} for i in items]

    payload = json.dumps(items, ensure_ascii=False)
    prompt = (
        "Ти — асистент будівельної компанії АрмаБуд. Нижче JSON зі свіжими матеріалами "
        f"з сайту DZO. Відбери ЛИШЕ релевантні під критерії: {FILTER_CRITERIA}.\n"
        "Для кожного релевантного поверни короткий запис. Якщо релевантних немає — порожній масив.\n"
        "Відповідай ВИКЛЮЧНО валідним JSON-масивом об'єктів виду "
        '{"title": "...", "summary": "1-2 речення суті", "deadline": "дата або \'\'", "url": "..."} '
        "без жодного тексту до чи після.\n\n"
        f"Матеріали:\n{payload}"
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={"model": MODEL, "max_tokens": 2000,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90,
    )
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", []))
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print("[warn] Claude повернув не-JSON; пропускаю фільтрацію.")
        return [{"title": i["title"], "summary": "", "deadline": "", "url": i["url"]}
                for i in items]


# ── Telegram ─────────────────────────────────────────────────────────
def send_telegram(digest):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("[warn] немає TELEGRAM_BOT_TOKEN/CHAT_ID — пропускаю надсилання.")
        return
    today = dt.date.today().strftime("%d.%m.%Y")
    lines = [f"<b>📡 DZO — дайджест за {today}</b>", ""]
    for it in digest:
        title = html.escape(it.get("title", "Без назви"))
        summary = html.escape(it.get("summary", ""))
        deadline = html.escape(it.get("deadline", ""))
        url = it.get("url", "")
        block = f"• <a href=\"{html.escape(url)}\"><b>{title}</b></a>"
        if summary:
            block += f"\n  {summary}"
        if deadline:
            block += f"\n  ⏳ Дедлайн: {deadline}"
        lines.append(block)
    text = "\n\n".join(lines)[:4000]  # ліміт Telegram

    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    ).raise_for_status()
    print(f"[ok] надіслано в Telegram: {len(digest)} запис(ів).")


# ── Стрічка для сайту ────────────────────────────────────────────────
def update_feed(digest):
    DATA_DIR.mkdir(exist_ok=True)
    old = []
    if FEED_PATH.exists():
        old = json.loads(FEED_PATH.read_text(encoding="utf-8")).get("items", [])
    now = dt.datetime.now().isoformat(timespec="minutes")
    fresh = [{**it, "added": now} for it in digest]
    items = (fresh + old)[:FEED_LIMIT]
    FEED_PATH.write_text(
        json.dumps({"updated": now, "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"[ok] оновлено стрічку сайту: {FEED_PATH}")


# ── Головний потік ───────────────────────────────────────────────────
def main():
    items = collect_items()
    print(f"[info] зібрано {len(items)} матеріал(ів) з DZO.")
    seen = load_seen()
    new_items = [i for i in items if i["url"] and i["url"] not in seen]
    print(f"[info] нових: {len(new_items)}.")

    if not new_items:
        print("[info] нового немає — завершую без надсилання.")
        return

    digest = summarize_with_claude(new_items)
    print(f"[info] після фільтрації Claude залишилось: {len(digest)}.")

    if digest:
        send_telegram(digest)
        update_feed(digest)

    # позначаємо ВСІ нові як побачені (навіть нерелевантні — щоб не повторювати)
    seen.update(i["url"] for i in new_items)
    save_seen(seen)
    print("[done]")


if __name__ == "__main__":
    main()

// ─────────────────────────────────────────────────────────────────────────
//  Інтеграція DZO-стрічки у застосунок «АрмаБуд — Менеджер проектів».
//
//  Бот (GitHub Actions) щодня оновлює data/dzo_feed.json у тому ж репозиторії.
//  Цей код підвантажує його і малює картки в розділі РАДАР / PROZORRO / новій
//  вкладці «DZO». Вставте у ваш фронтенд і викличте renderDzoFeed("dzo-feed").
//
//  update_feed() у dzo_monitor_prozorro.py ДОПИСУЄ нові тендери до старих
//  (обрізаючи список до 50), а не перезаписує з нуля — тож тендери з
//  минулим дедлайном самі не зникають із файлу. Тому тут на фронтенді:
//    • тендери з дедлайном у минулому не показуються (фільтр нижче);
//    • користувач може вручну приховати картку — id зберігається в
//      localStorage і картка не показується при наступних відрисовках.
// ─────────────────────────────────────────────────────────────────────────

const DZO_HIDDEN_KEY = "dzoHiddenTenderIds";

async function loadDzoFeed() {
  // відносний шлях у межах GitHub Pages цього ж репозиторію
  const res = await fetch("data/dzo_feed.json?_=" + Date.now()); // _ — обхід кешу
  if (!res.ok) throw new Error("Не вдалося завантажити dzo_feed.json");
  return res.json();
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// Стабільний id тендера — хвіст url після /tender/, без query-параметрів
// (на випадок, якщо колись до посилання додадуться ?utm чи інші параметри).
function dzoTenderId(item) {
  const m = /\/tender\/([^/?#]+)/.exec(item.url || "");
  return m ? m[1] : (item.url || item.title || "");
}

function dzoLoadHidden() {
  try {
    return new Set(JSON.parse(localStorage.getItem(DZO_HIDDEN_KEY) || "[]"));
  } catch (e) {
    return new Set();
  }
}

function dzoSaveHidden(idsSet) {
  localStorage.setItem(DZO_HIDDEN_KEY, JSON.stringify([...idsSet]));
}

function dzoHideTender(id) {
  const hidden = dzoLoadHidden();
  hidden.add(id);
  dzoSaveHidden(hidden);
}

function dzoClearHidden() {
  localStorage.removeItem(DZO_HIDDEN_KEY);
}

// deadline у feed-файлі — сирий рядок "YYYY-MM-DD HH:MM" з Prozorro
// (tenderPeriod.endDate, обрізаний), фактично UTC, але без позначки зони.
// new Date("...T00:00") у браузері трактує час як ЛОКАЛЬНИЙ, не UTC —
// тож явно дописуємо "Z", щоб порівняння з "зараз" було коректним.
function dzoDeadlinePassed(deadline) {
  if (!deadline) return false; // немає дедлайну — не приховуємо автоматично
  const iso = deadline.replace(" ", "T") + "Z";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return false; // не змогли розпарсити — краще показати
  return d.getTime() < Date.now();
}

async function renderDzoFeed(containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  try {
    const { updated, items: allItems } = await loadDzoFeed();
    const hidden = dzoLoadHidden();

    const notExpired = (allItems || []).filter(it => !dzoDeadlinePassed(it.deadline));
    const visible = notExpired.filter(it => !hidden.has(dzoTenderId(it)));
    const hiddenCount = notExpired.length - visible.length;

    if (!visible.length) {
      el.innerHTML = '<p class="muted">Поки що нових записів немає.</p>' +
        dzoFooterHtml(hiddenCount, containerId);
      dzoBindFooter(el, containerId);
      return;
    }

    const head = updated
      ? `<div class="dzo-updated">Оновлено: ${escapeHtml(updated)}</div>` : "";
    const cards = visible.map(it => {
      const id = dzoTenderId(it);
      return `
      <article class="dzo-card" data-dzo-id="${escapeHtml(id)}">
        <button class="dzo-hide" type="button" title="Приховати цей тендер">✕ Приховати</button>
        <a class="dzo-title" href="${escapeHtml(it.url)}" target="_blank" rel="noopener">
          ${escapeHtml(it.title)}
        </a>
        ${it.summary ? `<p class="dzo-summary">${escapeHtml(it.summary)}</p>` : ""}
        ${it.amount ? `<span class="dzo-amount">💰 ${escapeHtml(it.amount)}</span>` : ""}
        ${it.deadline ? `<span class="dzo-deadline">⏳ ${escapeHtml(it.deadline)}</span>` : ""}
        ${it.added ? `<time class="dzo-added">${escapeHtml(it.added)}</time>` : ""}
      </article>`;
    }).join("");

    el.innerHTML = head + cards + dzoFooterHtml(hiddenCount, containerId);

    el.querySelectorAll(".dzo-hide").forEach(btn => {
      btn.addEventListener("click", () => {
        const card = btn.closest(".dzo-card");
        const id = card && card.getAttribute("data-dzo-id");
        if (id) dzoHideTender(id);
        renderDzoFeed(containerId);
      });
    });
    dzoBindFooter(el, containerId);
  } catch (e) {
    el.innerHTML = `<p class="error">Помилка завантаження стрічки: ${escapeHtml(e.message)}</p>`;
  }
}

function dzoFooterHtml(hiddenCount, containerId) {
  if (!hiddenCount) return "";
  return `<div class="dzo-footer">
    <a href="#" class="dzo-link" data-dzo-action="clear">Показати приховані (${hiddenCount})</a>
  </div>`;
}

function dzoBindFooter(el, containerId) {
  const link = el.querySelector('[data-dzo-action="clear"]');
  if (!link) return;
  link.addEventListener("click", (e) => {
    e.preventDefault();
    dzoClearHidden();
    renderDzoFeed(containerId);
  });
}

// Приклад: document.addEventListener("DOMContentLoaded", () => renderDzoFeed("dzo-feed"));

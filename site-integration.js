// ─────────────────────────────────────────────────────────────────────────
//  Інтеграція DZO-стрічки у застосунок «АрмаБуд — Менеджер проектів».
//
//  Бот (GitHub Actions) щодня оновлює data/dzo_feed.json у тому ж репозиторії.
//  Цей код підвантажує його і малює картки в розділі РАДАР / PROZORRO / новій
//  вкладці «DZO». Вставте у ваш фронтенд і викличте renderDzoFeed("dzo-feed").
// ─────────────────────────────────────────────────────────────────────────

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

async function renderDzoFeed(containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  try {
    const { updated, items } = await loadDzoFeed();
    if (!items || !items.length) {
      el.innerHTML = '<p class="muted">Поки що нових записів немає.</p>';
      return;
    }
    const head = updated
      ? `<div class="dzo-updated">Оновлено: ${escapeHtml(updated)}</div>` : "";
    const cards = items.map(it => `
      <article class="dzo-card">
        <a class="dzo-title" href="${escapeHtml(it.url)}" target="_blank" rel="noopener">
          ${escapeHtml(it.title)}
        </a>
        ${it.summary ? `<p class="dzo-summary">${escapeHtml(it.summary)}</p>` : ""}
        ${it.deadline ? `<span class="dzo-deadline">⏳ ${escapeHtml(it.deadline)}</span>` : ""}
        ${it.added ? `<time class="dzo-added">${escapeHtml(it.added)}</time>` : ""}
      </article>`).join("");
    el.innerHTML = head + cards;
  } catch (e) {
    el.innerHTML = `<p class="error">Помилка завантаження стрічки: ${escapeHtml(e.message)}</p>`;
  }
}

// Приклад: document.addEventListener("DOMContentLoaded", () => renderDzoFeed("dzo-feed"));

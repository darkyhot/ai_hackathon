// Аккаунт: вход и регистрация, история, баннер о cookie.
// До входа всё, что делает посетитель, сохраняется на его cookie; при входе — переходит в аккаунт.
import { api } from "./api.js";
import { el, html, rub } from "./format.js";
import { openModal, setModalBody } from "./modal.js";

let me = { user: null, consent: true };
const slot = () => document.getElementById("account");

export async function initAccount() {
  try { me = await api.me(); } catch { return; }
  renderButton();
  if (!me.consent) showCookieBanner();
}

function renderButton() {
  const s = slot();
  if (!s) return;
  s.innerHTML = me.user
    ? html`<button class="account-btn" data-account="menu" title="${me.user.email}"><span class="avatar">${(me.user.name || me.user.email)[0].toUpperCase()}</span><span class="account-name">${me.user.name || me.user.email}</span></button>`
    : html`<button class="account-btn" data-account="login">Войти</button>`;
  s.querySelector("[data-account]").addEventListener("click", (e) => (e.currentTarget.dataset.account === "menu" ? openMenu() : openAuth("login")));
}

function showCookieBanner() {
  if (document.querySelector(".cookie-banner")) return;
  const b = el(html`
    <div class="cookie-banner" role="region" aria-label="Cookie">
      <p>Сохраняем историю поиска в cookie этого браузера, чтобы агент помнил ваши отметки и запросы. После регистрации она перейдёт в ваш аккаунт.</p>
      <button class="btn btn-primary btn-small">Понятно</button>
    </div>`);
  b.querySelector("button").addEventListener("click", async () => {
    b.remove();
    try { await api.consent(); } catch { /* не критично */ }
  });
  document.body.append(b);
}

function authForm(mode) {
  const reg = mode === "register";
  return html`
    <div class="auth-switch" role="tablist">
      <button type="button" class="${reg ? "" : "is-on"}" data-mode="login">Вход</button>
      <button type="button" class="${reg ? "is-on" : ""}" data-mode="register">Регистрация</button>
    </div>
    <form class="auth-form" novalidate>
      ${html.raw(reg ? html`<label>Как к вам обращаться<input name="name" autocomplete="name" maxlength="120"></label>` : "")}
      <label>Почта<input name="email" type="email" autocomplete="email" required></label>
      <label>Пароль<input name="password" type="password" autocomplete="${reg ? "new-password" : "current-password"}" minlength="8" required></label>
      ${html.raw(reg ? html`<p class="hint">Не короче 8 символов. Храним только хеш пароля.</p>` : "")}
      <p class="auth-error" hidden></p>
      <button class="btn btn-primary" type="submit">${reg ? "Зарегистрироваться" : "Войти"}</button>
      <button class="btn btn-ghost" type="button" data-sber>Войти по Сбер ID</button>
      <p class="hint">История поиска с этого браузера перейдёт в аккаунт.</p>
    </form>`;
}

export function openAuth(mode = "login") {
  const dlg = openModal(mode === "register" ? "Регистрация" : "Вход", authForm(mode));
  bindAuth(dlg, mode);
}

function bindAuth(dlg, mode) {
  dlg.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => {
    dlg.querySelector(".modal-head h2").textContent = b.dataset.mode === "register" ? "Регистрация" : "Вход";
    setModalBody(dlg, authForm(b.dataset.mode));
    bindAuth(dlg, b.dataset.mode);
  }));
  const form = dlg.querySelector("form");
  const err = dlg.querySelector(".auth-error");
  dlg.querySelector("[data-sber]").addEventListener("click", async () => {
    err.hidden = false;
    try { await api.sberId(); } catch (e) { err.textContent = e.message; }
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    const btn = form.querySelector("[type=submit]");
    btn.disabled = true;
    err.hidden = true;
    try {
      const r = mode === "register" ? await api.register(data) : await api.login(data);
      me.user = r.user;
      me.consent = true;
      document.querySelector(".cookie-banner")?.remove();
      renderButton();
      const moved = (r.claimed?.events || 0) + (r.claimed?.agent_sessions || 0);
      setModalBody(dlg, html`<p class="auth-ok">Готово, ${r.user.name || r.user.email}!</p>
        <p class="hint">${moved ? "История поиска с этого браузера теперь в вашем аккаунте." : "Теперь агент будет помнить ваши поиски."}</p>
        <button class="btn btn-primary" data-close>Продолжить</button>`);
      dlg.querySelector("[data-close]").addEventListener("click", () => dlg.close());
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
      btn.disabled = false;
    }
  });
}

const STATUS = { completed: "сделка", cancelled: "отменён", saving: "копим на взнос", empty: "ничего не нашлось", paused: "пауза",
  exhausted: "варианты закончились", running: "не завершён", error: "ошибка" };

async function openMenu() {
  const dlg = openModal("Мой кабинет", html`<p class="hint">Загружаю историю…</p>`, { wide: true });
  let h;
  try { h = await api.history(); } catch (e) { setModalBody(dlg, html`<p class="auth-error">${e.message}</p>`); return; }
  const sessions = h.sessions.map((s) => html`
    <li class="history-open" data-open-chat="${s.id}" data-title="${s.query}" role="button" tabindex="0" title="Открыть и продолжить диалог">
    <div><b>${s.query}</b><span class="hint">${s.started_at.replace("T", " ")} · ${STATUS[s.status] || s.status}</span></div>
    ${html.raw(s.summary ? html`<p class="hint">${s.summary}</p>` : "")}</li>`).join("");
  const liked = h.liked.map((x) => html`<li><a href="${x.url || "#"}" target="_blank" rel="noopener">${x.address}</a><span class="num">${rub(x.price)}</span></li>`).join("");
  setModalBody(dlg, html`
    <p class="account-who">${me.user.name ? `${me.user.name} · ` : ""}${me.user.email}</p>
    <div class="account-stats">
      <div><b class="num">${h.sessions.length}</b><span>поисков</span></div>
      <div><b class="num">${h.marks.like || 0}</b><span>понравилось</span></div>
      <div><b class="num">${h.calls}</b><span>звонков продавцам</span></div>
      <div><b class="num">${h.events}</b><span>действий в журнале</span></div>
    </div>
    <h3 class="sub">Диалоги — нажмите, чтобы открыть и продолжить</h3>
    ${html.raw(sessions ? html`<ul class="history">${html.raw(sessions)}</ul>` : html`<p class="hint">Пока пусто.</p>`)}
    ${html.raw(liked ? html`<h3 class="sub">Понравились</h3><ul class="history liked">${html.raw(liked)}</ul>` : "")}
    <div class="account-actions"><button class="btn btn-ghost" data-logout>Выйти</button></div>`);
  dlg.querySelectorAll("[data-open-chat]").forEach((li) => li.addEventListener("click", () => {
    dlg.close();
    window.dispatchEvent(new CustomEvent("open-chat", { detail: { id: li.dataset.openChat, title: li.dataset.title } }));
  }));
  dlg.querySelector("[data-logout]").addEventListener("click", async () => {
    await api.logout();
    me.user = null;
    renderButton();
    dlg.close();
  });
}

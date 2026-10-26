// Модальное окно на <dialog>: закрывается крестиком, Esc и кликом по фону.
import { el, html } from "./format.js";

export function openModal(title, body, { wide = false } = {}) {
  document.querySelector("dialog.modal")?.remove();
  const dlg = el(html`
    <dialog class="modal ${wide ? "is-wide" : ""}" aria-label="${title}">
      <header class="modal-head">
        <h2>${title}</h2>
        <button class="modal-x" type="button" aria-label="Закрыть">×</button>
      </header>
      <div class="modal-body">${html.raw(body)}</div>
    </dialog>`);
  document.body.append(dlg);
  dlg.querySelector(".modal-x").addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  dlg.addEventListener("close", () => dlg.remove());
  dlg.showModal();
  return dlg;
}

export function setModalBody(dlg, body) {
  const b = dlg.querySelector(".modal-body");
  if (b) b.innerHTML = body;
}

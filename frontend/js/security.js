// Окно проверки безопасности: клик по критерию — что проверено, как, чем подтверждено и как проверить самому.
import { api } from "./api.js";
import { html } from "./format.js";
import { openModal, setModalBody } from "./modal.js";

const STATUS = {
  ok: ["tag-green", "проверено по объявлению"], info: ["tag-blue", "к сведению"], warn: ["tag-amber", "есть оговорка"],
  partial: ["tag-amber", "частично"], not_checked: ["tag-gray", "не проверено"], done: ["tag-green", "проверено"],
};
const LEVEL = { high: ["tag-green", "высокая"], medium: ["tag-amber", "средняя"], low: ["tag-red", "низкая"] };

// какие факты из звонка относятся к критерию
const FACTS_FOR = {
  owner: ["owners"], egrn: ["owners"], history: ["owners"], registered: ["residents"],
  deadline_ad: ["deadline"], deadline: ["deadline"], sale_type: ["contract"], contract: ["contract"],
  escrow: ["contract"], replanning: ["condition"],
};

function items(d) {
  return [
    ...d.checks.map((c, i) => ({ ...c, id: `a${i}`, group: "По объявлению" })),
    ...d.before_deal.map((c) => ({ ...c, id: `r-${c.key}`, group: "До сделки по реестрам" })),
  ];
}

export function factBadge(f) {
  const [cls, label] = LEVEL[f.level] || LEVEL.medium;
  return html`<span class="tag ${cls}" title="Уверенность ${Math.round(f.confidence * 100)}%">уверенность: ${label}</span>`;
}

export function factRow(f) {
  return html`
    <li class="fact">
      <div class="fact-head"><b>${f.label}:</b> ${f.value} ${html.raw(factBadge(f))}</div>
      ${html.raw(f.quote ? html`<blockquote>«${f.quote}»</blockquote>` : "")}
      <p class="hint">${f.reasons.join("; ")}. Подтвердить: ${f.verify_with}.</p>
      ${html.raw(f.times > 1 ? html`<p class="hint">${f.consistent ? `Продавец отвечал так же в ${f.times} звонках.` : `В прошлых звонках говорил иначе: ${f.previous.join("; ")} — повод перепроверить.`}</p>` : "")}
    </li>`;
}

function detail(it, d) {
  const [cls, label] = STATUS[it.status] || STATUS.not_checked;
  const ev = (it.evidence || []).map((e) => html`<li><span>${e.label}</span><b>${e.value}</b></li>`).join("");
  const keys = FACTS_FOR[it.key] || [];
  const facts = (d.facts || []).filter((f) => keys.includes(f.key));
  const sc = it.self_check;
  return html`
    <div class="sec-detail">
      <div class="sec-detail-head"><h3>${it.title}</h3><span class="tag ${cls}">${label}</span></div>
      ${html.raw(it.note ? html`<p class="sec-note">${it.note}</p>` : "")}
      ${html.raw(it.shows ? html`<p><b>Что покажет:</b> ${it.shows}</p>` : "")}
      ${html.raw(it.why ? html`<p class="sec-why">Важно здесь: ${it.why}</p>` : "")}
      <h4>Как проверял агент</h4>
      <p>${it.method || "—"}</p>
      <h4>Источник</h4>
      <p>${html.raw(it.source_url ? html`<a href="${it.source_url}" target="_blank" rel="noopener">${it.source || it.source_url}</a> — откройте и сверьте сами` : html`${it.source || "—"}`)}</p>
      ${html.raw(ev ? html`<h4>Доказательства</h4><ul class="evidence">${html.raw(ev)}</ul>` : "")}
      ${html.raw(facts.length ? html`<h4>Что сказал продавец</h4><ul class="call-facts">${html.raw(facts.map(factRow).join(""))}</ul>` : "")}
      ${html.raw(sc ? html`<h4>Как проверить самому</h4>
        <p>${sc.how}</p>
        ${html.raw(sc.url ? html`<p><a class="btn btn-ghost btn-small" href="${sc.url}" target="_blank" rel="noopener">${sc.service} ↗</a></p>` : html`<p class="hint">${sc.service}</p>`)}` : "")}
    </div>`;
}

function body(d, current) {
  const list = items(d);
  const cur = list.find((x) => x.id === current) || list[0];
  let group = "";
  const nav = list.map((it) => {
    const [cls] = STATUS[it.status] || STATUS.not_checked;
    const head = it.group !== group ? html`<li class="sec-nav-group">${(group = it.group)}</li>` : "";
    return head + html`<li><button type="button" class="${it.id === cur.id ? "is-on" : ""}" data-item="${it.id}"><span class="dot ${cls}"></span>${it.title}</button></li>`;
  }).join("");
  const otherFacts = (d.facts || []).filter((f) => !Object.values(FACTS_FOR).flat().includes(f.key));
  return html`
    <div class="sec-modal">
      <nav><ul class="sec-nav">${html.raw(nav)}</ul></nav>
      <div>
        ${html.raw(detail(cur, d))}
        ${html.raw(otherFacts.length ? html`<details class="sec-more"><summary>Другие факты из звонка (${otherFacts.length})</summary><ul class="call-facts">${html.raw(otherFacts.map(factRow).join(""))}</ul></details>` : "")}
        <details class="sec-more"><summary>Почему агенту можно доверять</summary><ul>${html.raw((d.trust || []).map((t) => html`<li>${t}</li>`).join(""))}</ul></details>
      </div>
    </div>`;
}

export async function openSecurity(listingId, itemId, sid, title) {
  const dlg = openModal(title || "Проверка безопасности", html`<p class="hint">Загружаю…</p>`, { wide: true });
  let d;
  try { d = await api.security(listingId, sid); } catch (e) { setModalBody(dlg, html`<p class="auth-error">${e.message}</p>`); return; }
  const render = (id) => {
    setModalBody(dlg, body(d, id));
    dlg.querySelectorAll("[data-item]").forEach((b) => b.addEventListener("click", () => render(b.dataset.item)));
  };
  render(itemId);
}

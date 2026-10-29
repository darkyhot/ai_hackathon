// Контроллер: SSE-события агента -> чат, степпер, панели этапов.
import { initAccount } from "./account.js";
import { api } from "./api.js";
import { addGuard, addLine, createCall, renderCall, setPhase, setStatus, setTyping, showLearned, showSellerInput, updatePrices } from "./call.js";
import { savingsChart } from "./chart.js";
import { mountEconomics } from "./economics.js";
import { el, html, rub, sleep } from "./format.js";
import { initChats, refreshChats } from "./chats.js";
import { mountMap } from "./map.js";
import { PhoneCall, formatPhone, normalizePhone } from "./phone.js";
import * as V from "./views.js";
import { Talk } from "./talk.js";
import { bindMic, recorder, voice } from "./voice.js";

const STEPS = ["Запрос", "Финансы", "Поиск", "Подбор", "Проверка", "Звонок", "Просмотр", "Сделка", "Итог"];

const $ = (id) => document.getElementById(id);
const chatLog = $("chatLog");
const stage = $("stage");
const stepper = $("stepper");

let state = freshState();
let gen = 0; // поколение сессии: события старой сессии отбрасываются
let queue = [];
let pumping = false;
let cities = {}; // { featured: [...], total }
let phoneReady = false; // в .env есть ключ Exolve — можно звонить на настоящий телефон
let sellerPhoneDefault = ""; // SELLER_PHONE из .env
const HANGUP = "(положил трубку)"; // сервер понимает это как конец разговора

function freshState() {
  return {
    sid: null, es: null, running: false, streamEnded: false, results: {}, status: Array(10).fill("idle"), view: 0,
    call: null, phone: null, marks: { liked: new Set(), disliked: new Set() }, opened: {}, strategy: null, awaiting: null,
    cards: {}, lastSeq: 0, retries: 0, // карточки решений по виду; последнее полученное событие ленты
  };
}

// ------------------------------------------------------------------ чат

function addMsg(role, text) {
  const node = el(html`<div class="msg msg-${role}">${text}</div>`);
  chatLog.append(node);
  chatLog.scrollTop = chatLog.scrollHeight;
  return node;
}

function settle(card, label) {
  card.classList.add("is-done");
  card.append(el(html`<div class="verdict">Решение: ${label}</div>`));
}

// Решение уходит на сервер; карточку закрывает событие resolved из ленты — так же она выглядит и в истории.
async function decide(card, kind, approved, data, label) {
  card.querySelectorAll("button, input, select").forEach((x) => (x.disabled = true));
  const said = card.dataset.said; // решение сказано голосом — фраза попадёт в историю диалога
  delete card.dataset.said;
  try {
    await api.confirm(state.sid, kind, approved, { ...data, _label: label, ...(said ? { _said: said } : {}) });
    if (kind === "call" && approved && state.call) state.call.mode = data.mode;
  } catch (e) {
    card.querySelectorAll("button, input, select").forEach((x) => (x.disabled = false));
    addMsg("error", `Не удалось отправить решение: ${e.message}`);
  }
}

function financeForm(ev) {
  const d = ev.defaults;
  return html`
    <div class="fin-form">
      <label>Доход в месяц, ₽<input type="number" step="5000" min="0" name="monthly_income" value="${d.monthly_income}"></label>
      <label>Расходы в месяц, ₽<input type="number" step="5000" min="0" name="monthly_expenses" value="${d.monthly_expenses}"></label>
      <label>Накопления, ₽<input type="number" step="50000" min="0" name="savings" value="${d.savings}"></label>
      <label>Когда хотите купить
        <select name="horizon_months">
          ${html.raw([[3, "в ближайшие 3 месяца"], [6, "за полгода"], [12, "за год"], [24, "за 2 года"], [36, "за 3 года"]]
            .map(([v, t]) => html`<option value="${v}" ${v === d.horizon_months ? "selected" : ""}>${t}</option>`).join(""))}
        </select>
      </label>
      <fieldset><legend>Отношение к риску</legend>
        ${html.raw([["conservative", "Без риска — только вклады"], ["moderate", "Умеренно — часть в облигациях"], ["aggressive", "Готов к колебаниям ради дохода"]]
          .map(([v, t]) => html`<label class="radio"><input type="radio" name="risk" value="${v}" ${v === d.risk ? "checked" : ""}>${t}</label>`).join(""))}
      </fieldset>
    </div>`;
}

function familyForm(ev) {
  const d = ev.defaults;
  const hasCap = d.matcap > 0;
  return html`
    <div class="fin-form family-form">
      <label>Сколько детей
        <select name="children">
          ${html.raw([[0, "Детей нет"], [1, "1"], [2, "2"], [3, "3"], [4, "4"], [5, "5 и больше"]]
            .map(([v, t]) => html`<option value="${v}" ${v === d.children ? "selected" : ""}>${t}</option>`).join(""))}
        </select>
      </label>
      <label data-age>Возраст младшего, полных лет<input type="number" min="0" max="25" step="1" name="youngest_age" value="${d.youngest_age}" placeholder="например, 4"></label>
      <label class="radio"><input type="checkbox" name="has_matcap" ${hasCap ? "checked" : ""}>Есть неизрасходованный маткапитал</label>
      <label data-cap ${hasCap ? "" : "hidden"}>Сумма маткапитала, ₽<input type="number" min="0" step="1000" name="matcap" value="${d.matcap || d.matcap_default}"></label>
    </div>`;
}

function bindFamilyForm(card) {
  const kids = card.querySelector("[name=children]");
  const age = card.querySelector("[data-age]");
  const has = card.querySelector("[name=has_matcap]");
  const cap = card.querySelector("[data-cap]");
  const sync = () => { age.hidden = kids.value === "0"; cap.hidden = !has.checked; };
  kids.addEventListener("change", sync);
  has.addEventListener("change", sync);
  sync();
  return () => {
    const children = Number(kids.value);
    const ageVal = card.querySelector("[name=youngest_age]").value;
    return {
      children,
      youngest_age: children && ageVal !== "" ? Number(ageVal) : null,
      matcap: has.checked ? Number(card.querySelector("[name=matcap]").value) || 0 : 0,
    };
  };
}

function collectForm(card) {
  const data = {};
  card.querySelectorAll(".fin-form input, .fin-form select").forEach((i) => {
    if (i.type === "radio") { if (i.checked) data[i.name] = i.value; }
    else data[i.name] = i.tagName === "SELECT" ? Number(i.value) : Number(i.value);
  });
  return data;
}

function confirmCard(ev) {
  if (ev.kind === "selection") return; // решения подборки — на экране подбора
  const live = !ev.replay; // из истории — без голоса, звонка и фокуса
  const card = el(html`<div class="confirm confirm-${ev.kind}"><h4>${ev.title}</h4><p>${ev.text}</p><div data-extra></div><div class="actions"></div></div>`);
  const extra = card.querySelector("[data-extra]");
  const actions = card.querySelector(".actions");
  state.cards[ev.kind] = card;

  if (ev.kind === "clarify") {
    state.awaiting = "clarify";
    const chips = ev.chips || [];
    if (live) sayPrompt(chips.length ? `${ev.text} Например: ${chips.slice(0, 3).join(", или ")}.` : ev.text);
    extra.innerHTML = html`<p class="hint">Ответьте в поле ниже — текстом или голосом.</p>
      ${html.raw(chips.length ? html`<div class="quick">${html.raw(chips.map((c) => html`<button class="chip-btn" data-quick="${c}">${c}</button>`).join(""))}</div>` : "")}`;
    extra.querySelectorAll("[data-quick]").forEach((b) => b.addEventListener("click", () => sendText(b.dataset.quick)));
    chatLog.append(card);
    chatLog.scrollTop = chatLog.scrollHeight;
    if (live) $("query").focus();
    return;
  }

  let collect = () => ({});
  if (ev.kind === "red_lines") {
    const p = ev.payload;
    extra.innerHTML = html`
      <dl class="redlines">
        <dt>Город</dt><dd>${p.city}</dd>
        <dt>Комнат</dt><dd>${p.rooms === 0 ? "студия" : p.rooms}</dd>
        <dt>Агент соглашается сам</dt><dd>${p.authority_rule}</dd>
      </dl>
      <label style="margin-top:8px">Бюджет, ₽ — выше агент не пойдёт
        <input type="number" step="50000" min="1000000" value="${p.budget_max}" data-budget>
      </label>`;
    collect = () => ({ budget_max: Number(extra.querySelector("[data-budget]").value) || p.budget_max });
  }
  if (ev.kind === "finance") {
    extra.innerHTML = financeForm(ev);
    collect = () => collectForm(card);
  }
  if (ev.kind === "family") {
    extra.innerHTML = familyForm(ev);
    collect = bindFamilyForm(card);
  }
  if (ev.kind === "call" && phoneReady) {
    // регистрируемся в Exolve заранее: по клику звонок уходит сразу
    if (live) {
      if (!state.phone || state.phone.ended) state.phone = new PhoneCall();
      state.phone.connect().catch((e) => console.warn("Exolve:", e.message));
    }
    extra.innerHTML = html`<label class="phone-field">Ваш телефон — агент позвонит вам как продавцу
      <input type="tel" inputmode="tel" placeholder="+7 999 123-45-67" value="${storedPhone()}" data-phone autocomplete="tel"></label>`;
  }

  const buttons = {
    red_lines: [["Подтвердить рамки", true, {}], ["Изменить запрос", false, {}]],
    finance: ev.price ? [["Рассчитать стратегию", true, {}], ["Пропустить", false, {}]] : [["Посчитать бюджет", true, {}]],
    payment: [["Наличными — вся сумма есть", true, { payment: "cash" }], ["Ипотека, на взнос хватает", true, { payment: "mortgage_ready" }], ["Ипотека, взнос ещё копим", true, { payment: "mortgage_saving" }]],
    finance_choice: ev.gap ? [["Перейти к поиску квартиры", true, {}], ["Сначала накопить", false, {}]] : [["Перейти к поиску квартиры", true, {}]],
    family: [["Подобрать программу", true, {}], ["Пропустить", false, {}]],
    housing: [["Новостройки и вторичка", true, { housing: "both" }], ["Только новостройки", true, { housing: "new" }], ["Только вторичка", true, { housing: "secondary" }]],
    call: [
      ...(phoneReady ? [["Позвонить мне на телефон", true, { mode: "phone" }]] : []),
      ["Звонить — продавца играет ИИ", true, { mode: "auto" }], ["Звонить — продавец я (в чате)", true, { mode: "human" }], ["Пропустить объект", false, {}],
    ],
    price: [[`Подтвердить ${rub(ev.price)}`, true, {}], ["Отклонить, звонить дальше", false, {}]],
    object: [["Выбираю эту квартиру", true, {}], ["Ищем дальше", false, {}]],
    sign: [["Подписать электронно", true, {}], ["Отложить", false, {}]],
  }[ev.kind] || [["Подтвердить", true, {}], ["Отклонить", false, {}]];

  // голосом: что сказать вслух и как ответить
  const hint = voiceHint(ev);
  card.dataset.hint = hint;
  const spoken = {
    red_lines: () => `Проверим рамки: ${ev.payload.city}, ${ev.payload.rooms === 0 ? "студия" : `${ev.payload.rooms}-комнатная`}, бюджет до ${rub(ev.payload.budget_max)}. ${ev.text}`,
    finance: () => `${ev.title}.`,
    payment: () => `${ev.title}`,
    family: () => `${ev.title}. ${ev.text}`,
  }[ev.kind]?.() ?? `${ev.title}. ${ev.text}`;
  if (live) sayPrompt(`${spoken} ${hint}`);

  buttons.forEach(([label, approved, data], i) => {
    const b = el(html`<button class="btn ${i === 0 ? "btn-primary" : "btn-ghost"} btn-small">${label}</button>`);
    b.addEventListener("click", () => {
      if (data.mode === "phone") return dialSeller(card, label);
      if (ev.kind === "call" && state.phone) { state.phone.hangup(); state.phone = null; } // заготовленный звонок не нужен
      return decide(card, ev.kind, approved, { ...collect(), ...data }, label);
    });
    actions.append(b);
  });
  chatLog.append(card);
  chatLog.scrollTop = chatLog.scrollHeight;
  if (!live) return;
  actions.querySelector("button")?.focus({ preventScroll: true });
  if (matchMedia("(max-width: 860px)").matches) card.scrollIntoView({ block: "center", behavior: "smooth" });
}

/** Клиент решил (событие resolved): карточка закрывается с подписью решения. */
function resolved(ev) {
  const card = state.cards[ev.kind];
  if (ev.kind === "selection") {
    stage.querySelectorAll("[data-selection]").forEach((b) => (b.disabled = true));
  } else if (card && !card.classList.contains("is-done")) {
    if (ev.kind === "clarify") card.classList.add("is-done");
    else settle(card, ev.label || (ev.approved ? "да" : "нет"));
  }
  delete state.cards[ev.kind];
  if (state.awaiting === ev.kind) state.awaiting = null;
}

/** Агент этого диалога больше не работает (сервер перезапускался или диалог остановлен): старые кнопки не нажать. */
function staleCards() {
  for (const [kind, card] of Object.entries(state.cards)) {
    if (kind === "selection") continue; // кнопки подборки — на экране, их выключаем ниже
    if (kind === "clarify") card.classList.add("is-done");
    else settle(card, "не принято — напишите сообщение, чтобы продолжить диалог");
  }
  state.cards = {};
  state.awaiting = null;
  stage.querySelectorAll("[data-selection]").forEach((b) => (b.disabled = true));
}

// ------------------------------------------------------------------ голосовой режим

const talk = new Talk({
  onText: (text) => routeVoice(text),
  canListen: () => {
    if (state.phone?.session && !state.phone.ended) return false; // идёт звонок на телефон
    const c = state.call;
    if (c?.started && !c.ended) return c.mode === "human" && c.awaitingSeller; // в звонке слушаем только «продавца»
    return true;
  },
  onState: (st, detail) => renderTalk(st, detail),
  onLevel: (rms) => $("talkOrb").style.setProperty("--lvl", Math.min(1, rms * 14).toFixed(2)),
});

const phoneActive = () => Boolean(state.phone?.session && !state.phone.ended);

function sayPrompt(text) {
  state.lastPrompt = text;
  if (talk.on && !phoneActive()) talk.say(text);
}

function voiceHint(ev) {
  return {
    red_lines: "Скажите «да», если всё верно, или «изменить».",
    finance: ev.price ? "Скажите «посчитай» или «пропустить»." : "Скажите «посчитай» — цифры можно поправить на карточке.",
    payment: "Скажите: наличными, ипотека и взнос есть — или копим на взнос.",
    finance_choice: ev.gap ? "Скажите «к поиску» или «сначала накопить»." : "Скажите «к поиску».",
    family: "Заполните семью на карточке и скажите «подобрать» — или «пропустить».",
    housing: "Скажите: «и там, и там», «только новостройки» или «только вторичка».",
    call: phoneReady
      ? "Скажите: «позвони мне», «продавца сыграет ИИ», «продавец я» или «пропустить»."
      : "Скажите: «продавца сыграет ИИ», «продавец я» или «пропустить».",
    price: "Подтверждаете? Скажите «да» или «отклонить».",
    object: "Скажите «беру» или «ищем дальше».",
    sign: "Скажите «подписать» или «отложить».",
  }[ev.kind] || "Скажите «да» или «нет».";
}

const norm = (t) => t.toLowerCase().replace(/ё/g, "е").replace(/[.,!?…«»"]/g, " ").replace(/\s+/g, " ").trim();
const YES = /^(да|ага|угу|давай|хорошо|ладно|ок|окей|конечно|верно|все верно|поехали|подтвер|согласен|согласна|годится|подходит)/;
const NO = /^(нет|не надо|не нужно|не хочу|отмена|отмени)/;

// какую кнопку карточки нажать по фразе: [образец, часть надписи кнопки или её номер]
const VOICE_PICK = {
  red_lines: [[/измен|друг/, "Изменить"], [/подтвер/, "Подтвердить"], [NO, "Изменить"], [YES, "Подтвердить"]],
  finance: [[/пропус/, "Пропустить"], [NO, "Пропустить"], [/посчит|рассчит|бюджет|стратег/, 0], [YES, 0]],
  payment: [[/налич|вся сумма|целиком|сразу/, "Наличными"], [/коп|не хватает|нет взноса|накоп/, "копим"], [/ипотек|хватает|взнос есть|есть взнос/, "хватает"]],
  finance_choice: [[/накоп|коп/, "Сначала накопить"], [/поиск|ищ|квартир|дальше/, "Перейти"], [YES, "Перейти"]],
  family: [[/пропус|не надо|^нет/, "Пропустить"], [/подбер|подобр|посчит|готово|заполн/, "Подобрать"], [YES, "Подобрать"]],
  housing: [[/там и там|обе|оба|везде|и то и|и новостр\w* и втор|и втор\w* и новостр/, "Новостройки и вторичка"],
            [/втор/, "Только вторичка"], [/новостро|первичк|застройщ/, "Только новостройки"]],
  call: [
    [/пропус|не звони|друг(ой|ую|ая)/, "Пропустить"],
    [/продавец (я|буду)|я продавец|я сыграю|я сам|сам буду|в чате/, "продавец я"],
    [/(^|\s)ии(\s|$)|нейросет|искусствен|робот|сыгра|играет|автомат/, "играет ИИ"],
    [/телефон|позвони мне|мне позвони|на мой|набери/, "Позвонить мне"],
    [YES, 0],
  ],
  price: [[/не подтвер|отклон|дорого|дальше|^нет/, "Отклонить"], [/подтвер|беру|соглас/, "Подтвердить"], [YES, "Подтвердить"]],
  object: [[/дальше|не (беру|нрав|подход)|^нет/, "Ищем дальше"], [/беру|выбира|эту/, "Выбираю"], [YES, "Выбираю"]],
  sign: [[/отлож|позже|потом|^нет/, "Отложить"], [/подпис/, "Подписать"], [YES, "Подписать"]],
};

function voiceButton(card, t) {
  const kind = [...card.classList].find((c) => c.startsWith("confirm-"))?.slice(8);
  const buttons = [...card.querySelectorAll(".actions button:not(:disabled)")];
  const rules = VOICE_PICK[kind] || [[NO, 1], [YES, 0]];
  for (const [re, target] of rules) {
    if (!re.test(t)) continue;
    const b = typeof target === "number" ? buttons[target] : buttons.find((x) => x.textContent.includes(target));
    if (b) return b;
  }
  return null;
}

const ORDINALS = [["перв", 1], ["втор", 2], ["трет", 3], ["четв", 4], ["пят", 5], ["шест", 6], ["седьм", 7], ["восьм", 8], ["девят", 9], ["десят", 10]];

// «вторая нравится», «первая и третья не то» -> отметки в подборке
function voteByVoice(t) {
  const cards = state.results[4]?.cards || [];
  const down = /не то|не нрав|не подход|не хоч|убер|минус|плох|мимо/.test(t);
  const up = !down && /нрав|подход|отлич|хорош|лайк|плюс|беру|отмет|интерес|супер|класс/.test(t);
  if (!up && !down) return false;
  const nums = new Set();
  for (const [stem, n] of ORDINALS) if (new RegExp(`(^|\\s)${stem}\\w*`).test(t)) nums.add(n);
  for (const m of t.matchAll(/(?:номер|№)\s*(\d+)/g)) nums.add(Number(m[1]));
  const ids = [...nums].map((n) => cards[n - 1]?.id).filter(Boolean);
  ids.forEach((id) => setVote(id, up ? "up" : "down", false));
  if (ids.length) talk.say(up ? "Отметил." : "Убрал.");
  return ids.length > 0;
}

async function routeVoice(text) {
  const t = norm(text);
  if (/^(стоп|хватит|выключи (голос|микрофон)|заверши(ть)? разговор)/.test(t)) return stopTalk();
  const c = state.call;
  if (c?.started && !c.ended && c.mode === "human" && c.awaitingSeller) {
    c.awaitingSeller = false;
    c.root?.querySelector("[data-seller-slot]")?.replaceChildren();
    return submitSeller(text);
  }
  const card = [...chatLog.querySelectorAll(".confirm:not(.is-done):not(.confirm-clarify)")].pop();
  if (card && card.querySelector(".actions button:not(:disabled)")) {
    const b = voiceButton(card, t);
    if (!b) return talk.say(`Не понял. ${card.dataset.hint || ""}`);
    card.dataset.said = text; // фраза придёт в ленту вместе с решением
    b.click();
    return;
  }
  if (state.awaiting === "selection") {
    if (voteByVoice(t)) return addMsg("user", text);
    if (/звони|позвони|созвон|продавц|поехали/.test(t)) return selectionAction("proceed", text);
  }
  return sendText(text);
}

const TALK_TEXT = {
  listening: ["Слушаю", "Говорите — отвечу, когда замолчите"],
  hearing: ["Слышу вас…", "Договорите — я подожду паузу"],
  processing: ["Распознаю…", ""],
  thinking: ["Агент думает…", "Ответит через пару секунд"],
  speaking: ["Агент говорит", "Нажмите на кружок, чтобы перебить"],
  waiting: ["Идёт звонок", "Продолжим, когда звонок закончится"],
};

function renderTalk(st, detail = "") {
  const panel = $("talkPanel");
  panel.dataset.state = st;
  const [title, sub] = TALK_TEXT[st] || ["", ""];
  $("talkState").textContent = title;
  $("talkHint").textContent = detail || (st === "listening" && state.running ? "Можно ответить или добавить пожелание" : sub);
  if (st !== "listening" && st !== "hearing") $("talkOrb").style.setProperty("--lvl", "0");
}

async function startTalk() {
  voice.stop();
  try {
    await talk.start();
  } catch (e) {
    addMsg("error", e.message);
    return;
  }
  $("composer").hidden = true;
  $("talkPanel").hidden = false;
  $("talkBtn").setAttribute("aria-pressed", "true");
  const card = [...chatLog.querySelectorAll(".confirm:not(.is-done)")].pop();
  if (card && state.lastPrompt) talk.say(state.lastPrompt);
  else if (state.awaiting === "selection") talk.say(SELECTION_PROMPT);
  else if (!state.running) talk.say("Слушаю. Расскажите, какую квартиру ищете: город, бюджет, комнаты и что для вас важно.");
  else talk.say("Слушаю.");
}

function stopTalk() {
  talk.stop();
  $("composer").hidden = false;
  $("talkPanel").hidden = true;
  $("talkBtn").setAttribute("aria-pressed", "false");
}

const SELECTION_PROMPT = "Подборка на экране. Скажите, что нравится — например, «вторая нравится» или «третья не то», "
  + "— или добавьте пожелание. Когда будете готовы, скажите «звони».";

// ------------------------------------------------------------------ степпер и сцена

function renderStepper() {
  stepper.innerHTML = STEPS.map((t, i) => {
    const n = i + 1;
    const st = state.status[n];
    const cls = [st === "active" ? "is-active" : "", st === "done" ? "is-done" : "", state.view === n ? "is-viewing" : ""].join(" ");
    const dot = st === "done" ? "✓" : n;
    return html`<li class="${cls}"><button data-step="${n}" ${st === "idle" ? "disabled" : ""} aria-current="${state.view === n ? "step" : "false"}"><span class="dot">${dot}</span>${t}</button></li>`;
  }).join("");
}

function viewFor(n) {
  const r = state.results;
  switch (n) {
    case 1: return r[1] ? V.profile(r[1]) : V.waiting("Разбираю запрос", "Город, бюджет, комнаты и ваши пожелания. Если чего-то не хватит — спрошу.");
    case 2: return r[2] ? V.finance(r[2], state.opened, state.strategy) : V.waiting("Финансы", "Ответьте в чате, как планируете покупать: наличными, в ипотеку или ещё копите на взнос.");
    case 3: return r[3] ? V.search(r[3]) : V.waiting("Ищу объявления", "Запрашиваю Циан и считаю справедливую цену по похожим квартирам.");
    case 4: return r[4] ? V.selection(r[4], state.marks) : V.waiting("Подбор", "Готовлю подборку.");
    case 5: return r[5] ? V.legal(r[5]) : V.waiting("Проверка", "Смотрю риски по данным объявления.");
    case 7: return r[7] ? V.viewing(r[7]) : V.waiting("Просмотр", "Договариваюсь о времени.");
    case 8: return r[8] ? V.deal(r[8]) : V.waiting("Оформление", "Собираю план сделки.");
    case 9: return r[9] ? V.summary(r[9]) : V.waiting("Итог", "Завершаю сделку.");
    default: return V.welcome(cities);
  }
}

function renderStage() {
  stage.innerHTML = "";
  if (state.view === 6) {
    stage.append(state.call ? renderCall(state.call, submitSeller) : el(V.waiting("Звонок", "Готовлю звонок продавцу.")));
  } else {
    stage.innerHTML = viewFor(state.view);
    const chart = stage.querySelector("[data-chart]");
    if (chart && state.results[2]?.finance) savingsChart(chart, state.results[2].finance, chart.dataset.selected);
    const map = stage.querySelector("[data-map]");
    if (map && state.results[4]?.cards) mountMap(map, state.results[4].cards);
    syncSelectionButtons();
  }
  renderStepper();
}

/** Кнопки подборки активны, только пока агент ждёт выбора. */
function syncSelectionButtons() {
  stage.querySelectorAll("[data-selection]").forEach((b) => (b.disabled = state.awaiting !== "selection"));
}

function show(n) {
  if (state.view !== n) {
    state.view = n;
    renderStage();
  } else renderStepper();
}

// ------------------------------------------------------------------ настоящий звонок (МТС Exolve)

function storedPhone() {
  try { return localStorage.getItem("sellerPhone") || sellerPhoneDefault; } catch { return sellerPhoneDefault; }
}

async function dialSeller(card, label) {
  const input = card.querySelector("[data-phone]");
  const number = normalizePhone(input?.value);
  card.querySelector(".mic-error")?.remove();
  const note = (text, isError = false) => {
    card.querySelector(".dial-note")?.remove();
    card.querySelector(".actions").before(el(html`<p class="dial-note ${isError ? "mic-error" : ""}">${text}</p>`));
  };
  if (!number) { note("Введите российский номер: +7 и 10 цифр.", true); input?.focus(); return; }
  try { localStorage.setItem("sellerPhone", input.value.trim()); } catch { /* приватный режим */ }
  const buttons = card.querySelectorAll("button, input");
  buttons.forEach((x) => (x.disabled = true));
  voice.stop();
  const phone = state.phone && !state.phone.ended ? state.phone : new PhoneCall();
  state.phone = phone;
  const steps = {
    connecting: "Подключаюсь к МТС Exolve…",
    dialing: `Звоню на ${formatPhone(number)}…`,
    ringing: `Звоню на ${formatPhone(number)} — возьмите трубку. Вы — продавец квартиры.`,
    answered: "Трубку взяли — агент начинает разговор.",
  };
  try {
    await phone.dial(number, (st) => note(steps[st] || st));
  } catch (e) {
    if (state.phone === phone) state.phone = null;
    buttons.forEach((x) => (x.disabled = false));
    note(e.message, true);
    return;
  }
  // продавец положил трубку: ближайшее ожидание ответа (listenPhone) сообщит агенту
  phone.onEnded = () => {
    if (state.phone === phone && state.call && !state.call.ended) addMsg("note", "Продавец положил трубку.");
  };
  await decide(card, "call", true, { mode: "phone" }, `${label}: ${formatPhone(number)}`);
}

async function speakPhone(text) {
  try {
    await state.phone.say(text);
  } catch (e) {
    addMsg("error", `Агент не смог произнести фразу: ${e.message}`);
  }
}

async function listenPhone(call, myGen) {
  const phone = state.phone;
  if (!phone || phone.ended) return submitSeller(HANGUP);
  const r = await phone.listen((st) => {
    if (gen !== myGen) return;
    setTyping(call, st === "listening" ? "seller" : "agent");
    setStatus(call, st === "listening" ? "Идёт звонок · слушаю продавца" : "Идёт звонок · распознаю ответ");
  });
  if (gen !== myGen || call.ended) return;
  setStatus(call, "Идёт звонок · запись");
  if (r.text) return submitSeller(r.text);
  if (r.ended) return submitSeller(HANGUP);
  // не распознали — даём ответить текстом, разговор не теряется
  addMsg("error", `${r.error} Впишите ответ продавца вручную.`);
  showSellerInput(call, submitSeller);
}

// ------------------------------------------------------------------ события агента

async function handle(ev, myGen) {
  const call = state.call;
  const quiet = Boolean(ev.replay); // история диалога: рисуем сразу, без пауз, голоса и звонка
  const pause = (ms) => (quiet ? null : sleep(ms));
  if (ev.seq) state.lastSeq = Math.max(state.lastSeq, ev.seq);
  switch (ev.type) {
    case "provider": setProvider(ev); break;
    case "message":
      addMsg(ev.role, ev.text);
      if (!quiet && ev.role === "agent" && talk.on && !phoneActive()) talk.say(ev.text);
      await pause(ev.role === "agent" ? 300 : 0);
      break;
    case "step":
      state.status[ev.step] = ev.status === "pending" ? "active" : ev.status;
      if (quiet) { if (ev.status === "active") state.view = ev.step; break; } // экран нарисуем один раз в конце истории
      if (ev.status === "active") show(ev.step); else renderStepper();
      await pause(150);
      break;
    case "result":
      state.results[ev.step] = ev.data;
      if (ev.kind === "finance") state.strategy = ev.data.finance?.risk || null;
      if (ev.kind === "selection") {
        state.marks = { liked: new Set(ev.data.liked || []), disliked: new Set(ev.data.disliked || []) };
      }
      state.view = ev.step;
      if (!quiet) renderStage();
      await pause(ev.kind === "selection" ? 0 : 500);
      break;
    case "confirm":
      if (ev.kind === "call") {
        state.call = createCall(ev);
        state.view = 6;
        if (!quiet) renderStage();
      }
      if (ev.kind !== "clarify") state.awaiting = ev.kind === "selection" ? "selection" : null;
      if (ev.kind === "selection") {
        state.cards.selection = true;
        syncSelectionButtons();
        if (!quiet) sayPrompt(SELECTION_PROMPT);
      }
      confirmCard(ev);
      break;
    case "resolved": resolved(ev); break;
    case "product_opened":
      state.opened[ev.product] = ev.amount;
      if (!quiet && state.view === 2) renderStage();
      break;
    case "call_start":
      if (!call) break;
      call.started = true;
      call.mode = ev.mode;
      if (quiet) { state.view = 6; break; }
      if (state.view !== 6) show(6); else renderStage();
      setStatus(call, "Идёт звонок · запись");
      setTyping(call, "agent");
      break;
    case "call_phase": if (call) setPhase(call, ev.phase); break;
    case "turn": {
      if (!call) break;
      if (ev.speaker === "seller" && ev.ask) call.ask = ev.ask;
      if (ev.speaker === "agent" && ev.offer) call.offer = ev.offer;
      call.awaitingSeller = false;
      const node = addLine(call, ev);
      updatePrices(call);
      if (quiet) break;
      node?.classList.add("is-speaking");
      const t0 = performance.now();
      if (call.mode === "phone") {
        if (ev.speaker === "agent" && state.phone && !state.phone.ended) await speakPhone(ev.text);
        else await sleep(300);
      } else {
        // в голосовом режиме звонок звучит всегда
        await voice.speak(ev.text, ev.speaker, talk.on);
        if (!voice.enabled && !talk.on) await sleep(Math.min(3000, 800 + ev.text.length * 16));
        else await sleep(Math.max(0, 300 - (performance.now() - t0)));
      }
      node?.classList.remove("is-speaking");
      if (gen !== myGen) return;
      if (!call.ended && !call.awaitingSeller) setTyping(call, ev.speaker === "agent" ? (call.mode === "human" ? null : "seller") : "agent");
      break;
    }
    case "price":
      if (!call) break;
      call.ask = ev.ask;
      if (ev.offer) call.offer = ev.offer;
      updatePrices(call);
      break;
    case "guard": if (call) addGuard(call, ev.text); break;
    case "await_seller":
      if (!call) break;
      if (quiet) { call.awaitingSeller = true; break; } // поле ответа покажем, если агент и сейчас ждёт (replay_end)
      if (call.mode === "phone") await listenPhone(call, myGen);
      else showSellerInput(call, submitSeller);
      break;
    case "call_end":
      if (!call) break;
      call.ended = true;
      call.awaitingSeller = false;
      call.status = ev.status;
      setTyping(call, null);
      setStatus(call, ev.status === "agreed" ? "Звонок завершён · договорились" : "Звонок завершён · ждём вашего решения", true);
      showLearned(call, ev.learned, ev.viewing);
      if (!quiet && state.phone) { state.phone.hangup(); state.phone = null; }
      break;
    case "done":
      if (quiet) { addMsg("note", DONE_NOTES[ev.status] || "Диалог завершён."); break; }
      finish(ev.status);
      break;
    case "error":
      addMsg("error", ev.text);
      if (quiet) break;
      if (talk.on) talk.say(`Ошибка: ${ev.text}`);
      finish("error");
      break;
    case "idle": // ответил на вопрос в завершённом диалоге — снова ждёт сообщения
      if (quiet) break;
      state.running = false;
      state.streamEnded = true;
      state.es?.close();
      setComposer();
      break;
    case "replay_end":
      // история показана; дальше — живые события, если агент работает
      state.running = ev.live;
      state.lastSeq = Math.max(state.lastSeq, ev.seq || 0);
      renderStage(); // экран этапа — один раз, по итогам истории
      if (!ev.live) {
        staleCards();
        state.es?.close();
      } else if (call?.awaitingSeller && !call.ended && call.mode === "human") {
        showSellerInput(call, submitSeller);
      }
      setComposer();
      refreshChats();
      break;
  }
}

function enqueue(ev) {
  queue.push({ ev, gen });
  if (!pumping) pump();
}

async function pump() {
  pumping = true;
  while (queue.length) {
    const { ev, gen: g } = queue.shift();
    if (g !== gen) continue;
    try { await handle(ev, g); } catch (e) { console.error(e); }
  }
  pumping = false;
}

async function submitSeller(text) {
  setTyping(state.call, "agent");
  try { await api.seller(state.sid, text); } catch (e) { addMsg("error", `Ответ продавца не отправлен: ${e.message}`); }
}

const DONE_NOTES = {
  completed: "Сделка завершена. Можно спросить что-то по ней или начать новый диалог.",
  cancelled: "Остановлено. Напишите, что поменять, — продолжу этот диалог.",
  paused: "Сделка на паузе. Напишите, когда будете готовы продолжить.",
  saving: "План накоплений сохранён. Когда взнос будет готов — напишите, продолжим подбор.",
  exhausted: "Отмеченные объекты закончились — напишите пожелания, подберу ещё.",
  empty: "Ничего не нашлось — напишите другой бюджет или город.",
};

function finish(status) {
  state.running = false;
  state.streamEnded = true;
  state.es?.close();
  setComposer();
  if (DONE_NOTES[status]) addMsg("note", DONE_NOTES[status]);
  refreshChats();
}

// ------------------------------------------------------------------ запуск и сообщения

function setComposer() {
  const inChat = Boolean(state.sid);
  $("sendBtn").textContent = inChat ? "Отправить" : "Поручить агенту";
  $("query").placeholder = state.running
    ? "Ответ агенту или пожелание: «кухня побольше», «не первый этаж»…"
    : inChat ? "Продолжить диалог: «а если трёшку?», «покажи дешевле»…" : "Например: ищу 2-комнатную в Казани до 9 млн рядом со школой";
  $("resetBtn").hidden = !inChat;
}

/** Закрыть текущий диалог на экране: звук, звонок, поток событий. */
function leaveChat() {
  state.es?.close();
  state.phone?.hangup();
  voice.stop();
  talk.interrupt();
  gen += 1;
  queue = [];
  state = freshState();
  chatLog.innerHTML = "";
  renderStage();
}

/** Лента диалога: история после after, затем живые события. Связь оборвалась — переподключаемся с того же места. */
function openStream(id, after) {
  state.es?.close();
  state.streamEnded = false;
  const myGen = gen;
  const es = api.chatStream(id, after);
  state.es = es;
  es.onmessage = (m) => {
    if (gen !== myGen) return;
    const ev = JSON.parse(m.data);
    state.retries = 0;
    if (ev.type === "done" && !ev.replay) state.streamEnded = true;
    if ((ev.type === "error" || ev.type === "idle") && !ev.replay) state.streamEnded = true;
    enqueue(ev);
  };
  es.onerror = () => {
    es.close();
    if (gen !== myGen || !state.running || state.streamEnded) return;
    if (state.retries++ < 5) {
      setTimeout(() => { if (gen === myGen && state.running) openStream(id, state.lastSeq); }, 1500 * state.retries);
    } else {
      addMsg("error", "Связь с агентом прервалась. Агент продолжает работу — откройте диалог позже из списка «Диалоги».");
      state.running = false;
      setComposer();
    }
  };
}

function setHash(id) {
  const want = id ? `#chat-${id}` : "";
  if (location.hash !== want) history.replaceState(null, "", want || location.pathname + location.search);
}

async function start(query) {
  if (!query.trim()) return;
  leaveChat();
  $("query").value = "";
  try {
    const { id } = await api.start(query);
    state.sid = id;
    state.running = true;
    setHash(id);
    setComposer();
    openStream(id, 0);
  } catch (e) {
    addMsg("error", `Агент не запустился: ${e.message}`);
  }
}

/** Открыть диалог из списка или по ссылке #chat-<id>. */
async function openChat(id) {
  if (!id || id === state.sid) return;
  leaveChat();
  state.sid = id;
  state.running = true; // до replay_end: он скажет, работает ли агент
  setHash(id);
  setComposer();
  openStream(id, 0);
}

/** Новый диалог: пустой экран, приветствие. */
function newChat() {
  leaveChat();
  setHash(null);
  addMsg("agent", "Опишите, какую квартиру ищете: город, бюджет, комнаты и что для вас важно.");
  setComposer();
  refreshChats();
}


async function sendText(text) {
  text = text.trim();
  if (!text) return;
  if (!state.sid) return start(text);
  $("query").value = "";
  const marks = { liked: [...state.marks.liked], disliked: [...state.marks.disliked] };
  try {
    const r = await api.chatMessage(state.sid, text, marks);
    if (r.continued) {
      // агент уже закончил — продолжает этот же диалог новым запуском: подключаемся к ленте с того же места
      state.running = true;
      state.cards = {};
      setComposer();
      openStream(state.sid, state.lastSeq);
      return;
    }
    if (state.awaiting === "clarify") {
      chatLog.querySelectorAll(".confirm-clarify:not(.is-done)").forEach((c) => c.classList.add("is-done"));
    }
    state.awaiting = null;
  } catch (e) {
    addMsg("error", `Сообщение не отправлено: ${e.message}`);
  }
}

/** said — фраза, если решение сказано голосом (попадёт в историю диалога). */
async function selectionAction(action, said = null) {
  if (state.awaiting !== "selection") return;
  if (action === "proceed" && !state.marks.liked.size) {
    if (said) talk.say("Вы ничего не отметили — позвоню по трём лучшим вариантам.");
    else if (!confirm("Вы ничего не отметили. Позвонить по трём лучшим вариантам?")) return;
  }
  state.awaiting = null;
  stage.querySelectorAll("[data-selection]").forEach((b) => (b.disabled = true));
  const label = action === "proceed" ? "звонить по отмеченным" : "подобрать ещё";
  try {
    await api.confirm(state.sid, "selection", true, {
      action, liked: [...state.marks.liked], disliked: [...state.marks.disliked], _label: label, ...(said ? { _said: said } : {}),
    });
  } catch (e) {
    state.awaiting = "selection";
    stage.querySelectorAll("[data-selection]").forEach((b) => (b.disabled = false));
    addMsg("error", `Не удалось отправить отметки: ${e.message}`);
  }
}

function setProvider(p) {
  const node = $("provider");
  const names = { deepseek: "DeepSeek", gigachat: "GigaChat", offline: "Без модели (правила)" };
  const degraded = p.degraded || p.provider === "offline";
  node.textContent = p.degraded ? `${names[p.provider] || p.provider} · запасной режим` : names[p.provider] || p.provider;
  node.title = p.reason ? `Модель недоступна: ${p.reason}` : `Модель: ${p.model}`;
  node.classList.toggle("is-offline", degraded);
}

// ------------------------------------------------------------------ обработчики UI

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  sendText($("query").value);
});
$("query").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});
$("resetBtn").addEventListener("click", newChat);

bindMic($("micBtn"), $("query"), {
  onFinal: (text) => sendText(text),
  onError: (message) => addMsg("error", message),
});
$("micBtn").hidden = false;

stage.addEventListener("click", async (e) => {
  const ex = e.target.closest("[data-example]");
  if (ex) return start(ex.dataset.example);

  const vote = e.target.closest("[data-vote]");
  if (vote) return setVote(vote.closest("[data-id]").dataset.id, vote.dataset.vote, true);
  const sel = e.target.closest("[data-selection]");
  if (sel) return selectionAction(sel.dataset.selection);

  const strat = e.target.closest("[data-strategy]");
  if (strat) {
    state.strategy = strat.dataset.strategy;
    return renderStage();
  }
  const open = e.target.closest("[data-open]");
  if (open && state.sid) {
    open.disabled = true;
    try { await api.product(state.sid, open.dataset.open, Number(open.dataset.amount)); }
    catch (err) { open.disabled = false; addMsg("error", `Не удалось: ${err.message}`); }
  }
});
/** Отметка «нравится / не то». toggle — повторное нажатие снимает отметку (клик); голосом — просто ставим. */
function setVote(id, dir, toggle) {
  const { liked, disliked } = state.marks;
  const up = dir === "up";
  const set = up ? liked : disliked;
  const other = up ? disliked : liked;
  if (toggle && set.has(id)) set.delete(id); else { set.add(id); other.delete(id); }
  const card = stage.querySelector(`.offer[data-id="${CSS.escape(id)}"]`);
  if (card) {
    card.classList.toggle("is-liked", liked.has(id));
    card.classList.toggle("is-disliked", disliked.has(id));
    card.querySelector('[data-vote="up"]')?.setAttribute("aria-pressed", String(liked.has(id)));
    card.querySelector('[data-vote="down"]')?.setAttribute("aria-pressed", String(disliked.has(id)));
  }
  const counter = stage.querySelector("[data-liked-count]");
  if (counter) counter.textContent = `(${liked.size})`;
}

$("talkBtn").addEventListener("click", () => (talk.on ? stopTalk() : startTalk()));
$("talkEnd").addEventListener("click", stopTalk);
$("talkOrb").addEventListener("click", () => {
  if (talk.busy) talk.interrupt();
  else talk.capture?.stop(); // договорил — отправить сразу, не дожидаясь паузы
});

stepper.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-step]");
  if (b && !b.disabled) show(Number(b.dataset.step));
});

const voiceBtn = $("voiceToggle");
const syncVoice = () => voiceBtn.setAttribute("aria-pressed", String(voice.enabled));
voiceBtn.hidden = !voice.supported;
voiceBtn.addEventListener("click", () => { voice.enabled = !voice.enabled; syncVoice(); });
syncVoice();

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    const which = tab.dataset.tab;
    document.querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("is-active", t === tab);
      t.setAttribute("aria-selected", String(t === tab));
    });
    $("view-deal").hidden = which !== "deal";
    $("view-economics").hidden = which !== "economics";
    if (which === "economics") mountEconomics($("view-economics"));
  }),
);

const fromHash = () => location.hash.match(/^#chat-([0-9a-f]{12})$/)?.[1];
window.addEventListener("hashchange", () => { const id = fromHash(); if (id) openChat(id); });
initChats({ open: openChat, create: newChat, current: () => state.sid });
initAccount({ onChange: () => refreshChats() });
if (fromHash()) openChat(fromHash());
else {
  addMsg("agent", "Здравствуйте! Я агент покупки квартиры. Расскажите текстом или голосом, что ищете: город, бюджет, комнаты и что для вас важно.");
  setComposer();
  renderStage();
}
api.health().then((h) => {
  cities = { featured: h.featured_cities || [], total: (h.cities || []).length };
  recorder.available = Boolean(h.stt?.installed && h.stt.status !== "error");
  phoneReady = Boolean(h.phone?.configured);
  sellerPhoneDefault = h.phone?.seller_phone || "";
  setProvider(h);
  if (state.view === 0) renderStage();
})
  .catch(() => setProvider({ provider: "offline", reason: "сервер недоступен" }));

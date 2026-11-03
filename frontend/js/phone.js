// Настоящий звонок продавцу через Voximplant.
// Браузер входит в приложение Voximplant (Web SDK) и «звонит» в сценарий VoxEngine (backend/voximplant/scenario.js),
// а уже облако набирает продавца, произносит реплики агента (синтез речи Voximplant) и распознаёт ответы (ASR Voximplant).
// Браузер шлёт сценарию тексты реплик и получает распознанный текст. Звука на сайте нет, микрофон не нужен.
// Пароль пользователя Voximplant остаётся на сервере: вход по одноразовому ключу, хеш считает сервер.

const SDK_URL = "/vendor/voximplant.min.js";
const CALL_TARGET = "seller"; // любое имя: правило приложения отправляет все звонки в сценарий

// SIP-коды, которые сценарий пересылает из CallEvents.Failed
const FAIL = {
  402: "Звонок не прошёл: на счёте Voximplant не хватает денег.",
  403: "Voximplant не пропустил звонок (403). Если включён тестовый режим (VOXIMPLANT_CALLER_ID=default) — звонить можно только на личный номер, подтверждённый в кабинете. Иначе номер, с которого звоним, не подтверждён. Проверьте: .venv/bin/python -m backend.phone",
  404: "Номер не найден.",
  408: "Никто не взял трубку.",
  480: "Номер недоступен.",
  484: "Номер набран не полностью.",
  486: "Номер занят.",
  487: "Звонок отменён.",
  603: "Звонок отклонили.",
};

let config = null;
let sdk = null;
let sdkLoading = null;
let sdkReady = null;

function pinGet() { try { return localStorage.getItem("phonePin") || ""; } catch { return ""; } }
function pinSet(v) { try { if (v) localStorage.setItem("phonePin", v); else localStorage.removeItem("phonePin"); } catch { /* приватный режим */ } }
const pinHeaders = () => (pinGet() ? { "X-Phone-Pin": pinGet() } : {});

async function phoneFetch(url, options = {}) {
  let res = await fetch(url, { ...options, headers: { ...(options.headers || {}), ...pinHeaders() } });
  if (res.status === 403) {
    const pin = prompt("PIN для звонков (задан на сервере в PHONE_PIN):");
    if (!pin) throw new Error("Звонок отменён: нужен PIN.");
    pinSet(pin.trim());
    res = await fetch(url, { ...options, headers: { ...(options.headers || {}), ...pinHeaders() } });
    if (res.status === 403) pinSet("");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Сервер не отдал настройки звонка (${res.status})`);
  return data;
}

export async function phoneConfig() {
  config ??= await phoneFetch("/api/phone");
  return config;
}

/** «8 (999) 123-45-67» -> «79991234567»; null, если это не российский номер. */
export function normalizePhone(raw) {
  let d = String(raw || "").replace(/\D/g, "");
  if (d.length === 11 && d[0] === "8") d = `7${d.slice(1)}`;
  if (d.length === 10) d = `7${d}`;
  return d.length === 11 && d[0] === "7" ? d : null;
}

export const formatPhone = (d) => `+${d[0]} ${d.slice(1, 4)} ${d.slice(4, 7)}-${d.slice(7, 9)}-${d.slice(9)}`;

/** Web SDK грузится только для звонка: 400 КБ не нужны тем, кто не звонит. */
function loadSdk() {
  if (window.VoxImplant) return Promise.resolve(window.VoxImplant);
  sdkLoading ??= new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = SDK_URL;
    s.onload = () => (window.VoxImplant ? resolve(window.VoxImplant) : reject(new Error("Web SDK Voximplant не загрузился.")));
    s.onerror = () => { sdkLoading = null; reject(new Error("Не удалось загрузить Web SDK Voximplant.")); };
    document.head.append(s);
  });
  return sdkLoading;
}

async function client(cfg) {
  sdk ??= await loadSdk();
  const c = sdk.getInstance();
  // микрофон не нужен: говорит облако (синтез речи), браузер передаёт только текст.
  // node — узел аккаунта: российские аккаунты (voximplant.ru) подключаются через свой балансировщик
  const options = { micRequired: false, progressTone: false, showDebugInfo: false, showWarnings: false };
  if (cfg.node) options.node = sdk.ConnectionNode?.[cfg.node] ?? cfg.node;
  sdkReady ??= c.init(options)
    .catch((e) => { sdkReady = null; throw e; });
  await sdkReady;
  return c;
}

/** Вход по одноразовому ключу: ключ даёт Voximplant, хеш с паролем считает наш сервер. */
async function login(c, username) {
  const S = sdk.ClientState;
  if (c.getClientState() === S.LOGGED_IN) return;
  if (c.getClientState() !== S.CONNECTED) {
    const timer = new Promise((_, reject) => setTimeout(() => reject(new Error("Voximplant не ответил за 15 секунд.")), 15000));
    await Promise.race([c.connect(), timer]).catch((e) => {
      throw new Error(`Нет связи с Voximplant (${e?.message || e}). Проверьте интернет/VPN.`);
    });
  }
  let key;
  try {
    key = (await c.requestOneTimeLoginKey(username))?.key;
  } catch (e) {
    key = e?.key; // ключ приходит в AuthResult с кодом 302 — часть версий SDK отдаёт его через reject
  }
  if (!key) throw new Error("Voximplant не выдал ключ для входа.");
  const { hash } = await phoneFetch("/api/phone/login", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key }),
  });
  try {
    await c.loginWithOneTimeKey(username, hash);
  } catch (e) {
    throw new Error(e?.code === 401 || e?.code === 404
      ? "Voximplant не принял пользователя приложения: перезапустите сервер, он настроит приложение заново."
      : `Не удалось войти в Voximplant${e?.code ? ` (код ${e.code})` : ""}.`);
  }
}

// Короткие реплики, пока агент думает над ответом продавца: так пауза не кажется обрывом связи.
const FILLERS = ["Так, понял.", "Угу, секунду.", "Понял вас.", "Хорошо, секунду."];

export class PhoneCall {
  constructor() {
    this.call = null;
    this.ended = false;
    this.answered = false;
    this.onEnded = () => {};
    this.onAudioState = () => {};
    this.ready = null;    // вход в Voximplant (делаем заранее, чтобы звонок шёл сразу)
    this.nextId = 1;
    this.pending = new Map(); // id реплики -> resolve, когда облако её договорило
    this.heard = [];          // распознанные фразы продавца, которые агент ещё не забрал
    this.waiter = null;       // listen() ждёт новую фразу
    this.fillerNo = 0;
    this.dialing = null;
  }

  /** Звук теперь играет облако, а не браузер: разблокировать нечего (метод оставлен для совместимости). */
  unlockAudio() {
    if (this.ended) return Promise.reject(new Error("Звонок уже завершён."));
    this.onAudioState("running");
    return Promise.resolve();
  }

  /** Подключается и входит в Voximplant. Можно вызвать заранее — dial() тогда начнёт звонок сразу. */
  connect() {
    this.ready ??= (async () => {
      const cfg = await phoneConfig();
      const c = await client(cfg);
      await login(c, cfg.username);
      return { cfg, c };
    })().catch((e) => { this.ready = null; throw e; });
    return this.ready;
  }

  /** Звонит. Promise завершается, когда продавец взял трубку. */
  async dial(number, onState = () => {}) {
    onState("connecting");
    let cfg;
    let c;
    try {
      ({ cfg, c } = await this.connect());
      if (this.ended) throw new Error("Звонок отменён.");
    } catch (e) {
      this.hangup();
      throw e;
    }
    if (number === String(cfg.callerid || "").replace(/\D/g, "")) {
      this.hangup();
      throw new Error("Это номер, с которого звонит агент: на него же позвонить нельзя. Укажите другой телефон — например, коллеги.");
    }
    onState("dialing");
    const t0 = performance.now();
    const mark = (what) => console.info(`[звонок] ${what}: ${Math.round(performance.now() - t0)} мс`);
    const E = (sdk ?? window.VoxImplant).CallEvents;
    return new Promise((resolve, reject) => {
      this.dialing = { resolve, reject, onState, mark };
      this.call = c.call({
        number: CALL_TARGET,
        video: false,
        customData: JSON.stringify({ to: number, callerid: cfg.callerid, voice: cfg.voice, asr: cfg.asr }),
      });
      this.call.on(E.Connected, () => mark("сценарий принял звонок"));
      this.call.on(E.MessageReceived, (e) => this.#onMessage(e.text));
      this.call.on(E.Failed, (e) => this.#fail(`Звонок не прошёл: ${e.reason || e.code || "ошибка Voximplant"}.`));
      this.call.on(E.Disconnected, () => this.#finish());
    });
  }

  #onMessage(text) {
    let msg;
    try { msg = JSON.parse(text); } catch { return; }
    const d = this.dialing;
    switch (msg.type) {
      case "ringing":
        d?.mark("идут гудки");
        d?.onState("ringing");
        break;
      case "answered":
        this.answered = true;
        d?.mark("трубку взяли");
        d?.onState("answered");
        this.dialing = null;
        d?.resolve();
        break;
      case "said":
        this.pending.get(msg.id)?.();
        this.pending.delete(msg.id);
        break;
      case "heard":
        if (msg.text) this.heard.push(msg.text);
        this.waiter?.();
        break;
      case "failed":
        this.#fail(FAIL[msg.code] || msg.reason || `Звонок не прошёл (код ${msg.code}).`);
        break;
      case "error":
        console.warn(`Voximplant ${msg.what}:`, msg.text);
        break;
      case "ended":
        this.#finish();
        break;
      default:
        break;
    }
  }

  #fail(message) {
    const d = this.dialing;
    this.dialing = null;
    this.hangup();
    d?.reject(new Error(message));
  }

  #send(obj) {
    try { this.call?.sendMessage(JSON.stringify(obj)); } catch { /* звонок уже завершён */ }
  }

  /** Произносит фразу в линию (синтез речи Voximplant). Завершается, когда фраза прозвучала. */
  say(text) {
    if (this.ended || !text) return Promise.resolve();
    const id = this.nextId++;
    return new Promise((resolve) => {
      const timer = setTimeout(() => { this.pending.delete(id); resolve(); }, 60000); // страховка от потерянного «said»
      this.pending.set(id, () => { clearTimeout(timer); resolve(); });
      this.#send({ type: "say", id, text });
    });
  }

  /** Короткое «Так, понял.» — пока агент думает над ответом. */
  filler() {
    if (this.ended) return;
    this.say(FILLERS[this.fillerNo++ % FILLERS.length]);
  }

  /**
   * Ждёт ответ продавца (распознаёт облако) до паузы.
   * -> {text, error, ended}: ended — продавец положил трубку.
   */
  async listen(onState = () => {}) {
    if (this.ended) return { text: "", error: null, ended: true };
    onState("listening");
    const wait = (ms) => new Promise((resolve) => {
      const timer = setTimeout(() => { this.waiter = null; resolve(false); }, ms);
      this.waiter = () => { clearTimeout(timer); this.waiter = null; resolve(true); };
    });
    if (!this.heard.length) await wait(25000);
    if (this.ended && !this.heard.length) return { text: "", error: null, ended: true };
    if (!this.heard.length) return { text: "", error: "Продавец молчит." };
    // продавец может договаривать фразу частями: ждём короткую паузу и склеиваем
    while (!this.ended && (await wait(900)));
    onState("processing");
    const text = this.heard.splice(0).join(" ").trim();
    this.filler();
    return { text, error: null };
  }

  #finish() {
    if (this.ended) return;
    const d = this.dialing;
    this.hangup();
    if (d) d.reject(new Error("Звонок завершился до ответа продавца."));
    else this.onEnded();
  }

  hangup() {
    if (this.ended) return;
    this.ended = true;
    this.dialing = null;
    this.onAudioState("closed");
    this.#send({ type: "hangup" });
    try { this.call?.hangup(); } catch { /* уже завершён */ }
    for (const done of this.pending.values()) done();
    this.pending.clear();
    this.waiter?.();
  }
}

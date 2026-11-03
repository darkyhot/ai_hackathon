// Поддельный Web SDK Voximplant для сквозной проверки звонка без облака (tests/e2e/server.py --fake-voximplant).
// Отдаётся вместо /vendor/voximplant.min.js. Внутри страницы исполняет НАСТОЯЩИЙ backend/voximplant/scenario.js
// на имитации VoxEngine: «телефон продавца» берёт трубку, реплики агента «звучат» 150 мс, ответы продавца
// приходят как результат распознавания. Интерфейс — как у Web SDK 4.x: getInstance, init, connect,
// requestOneTimeLoginKey, loginWithOneTimeKey, call, Call.on / sendMessage / hangup.
(function () {
  "use strict";
  const ClientState = { DISCONNECTED: "DISCONNECTED", CONNECTING: "CONNECTING", CONNECTED: "CONNECTED", LOGGING_IN: "LOGGING_IN", LOGGED_IN: "LOGGED_IN" };
  const CallEvents = { Connected: "Connected", Disconnected: "Disconnected", Failed: "Failed", MessageReceived: "MessageReceived" };
  const FILLERS = new Set(["Так, понял.", "Угу, секунду.", "Понял вас.", "Хорошо, секунду."]);

  // что произошло — для проверок из теста (window.__vox)
  const vox = (window.__vox = { said: [], heard: [], dialed: null, callerid: null, login: null, log: [], seller: [] });
  try { vox.seller = JSON.parse(localStorage.getItem("fakeVoxSeller") || "[]"); } catch { /* по умолчанию — пусто */ }

  class Emitter {
    constructor() { this.handlers = {}; }
    on(ev, fn) { (this.handlers[ev] ||= []).push(fn); }
    addEventListener(ev, fn) { this.on(ev, fn); }
    emit(ev, payload = {}) { for (const fn of this.handlers[ev] || []) fn({ name: ev, call: this, ...payload }); }
  }

  // ------------------------------------------------------------ имитация VoxEngine (облако)
  async function runScenario(sdkCall, customData) {
    const code = await (await fetch("/__fake_vox/scenario.js")).text();
    let alerting = null;
    let sellerTurn = 0;
    let awaitingReply = false;
    const V = {
      CallEvents: { Connected: "Call.Connected", Disconnected: "Call.Disconnected", Failed: "Call.Failed", Ringing: "Call.Ringing", MessageReceived: "Call.MessageReceived", PlaybackFinished: "Call.PlaybackFinished" },
      ASREvents: { Result: "ASR.Result", ASRError: "ASR.Error", Stopped: "ASR.Stopped" },
    };
    class VCall extends Emitter {
      constructor(kind) { super(); this.kind = kind; }
      emit(ev, payload = {}) { super.emit(ev, payload); }
    }
    const web = new VCall("web");
    web.answer = () => setTimeout(() => sdkCall.emit(CallEvents.Connected), 30);
    web.reject = () => sdkCall.emit(CallEvents.Failed, { code: 486, reason: "Busy" });
    web.hangup = () => setTimeout(() => sdkCall.emit(CallEvents.Disconnected), 10);
    web.sendMessage = (text) => setTimeout(() => sdkCall.emit(CallEvents.MessageReceived, { text }), 5);
    sdkCall._toCloud = (text) => web.emit(V.CallEvents.MessageReceived, { text });
    sdkCall._hangupCloud = () => web.emit(V.CallEvents.Disconnected);
    let asr = null;
    const pstn = new VCall("pstn");
    const sellerSays = () => {
      if (!awaitingReply || !asr) return;
      awaitingReply = false;
      if (sellerTurn >= vox.seller.length) { vox.log.push("seller hangs up"); setTimeout(() => pstn.emit(V.CallEvents.Disconnected), 200); return; }
      const text = vox.seller[sellerTurn++];
      vox.heard.push(text);
      setTimeout(() => asr.emit(V.ASREvents.Result, { text, confidence: 0.9 }), 250);
    };
    pstn.say = (text, params) => {
      vox.said.push({ text, voice: params && params.voice });
      if (!FILLERS.has(text)) awaitingReply = true; // на «Так, понял.» продавец не отвечает
      setTimeout(() => { pstn.emit(V.CallEvents.PlaybackFinished); setTimeout(sellerSays, 50); }, 150);
    };
    pstn.stopPlayback = () => vox.log.push("stopPlayback");
    pstn.sendMediaTo = (unit) => vox.log.push(`sendMediaTo ${unit.kind}`);
    pstn.hangup = () => vox.log.push("pstn hangup");
    const globals = {
      Modules: { ASR: "asr" },
      require: () => {},
      Logger: { write: (m) => vox.log.push(String(m)) },
      AppEvents: { CallAlerting: "Application.CallAlerting" },
      CallEvents: V.CallEvents,
      ASREvents: V.ASREvents,
      VoiceList: { SaluteSpeech: { ru_RU_sergey: "salute:sergey" } },
      ASRProfileList: { SaluteSpeech: { ru_RU: "salute-asr:ru" } },
      VoxEngine: {
        addEventListener: (ev, fn) => { if (ev === "Application.CallAlerting") alerting = fn; },
        callPSTN: (number, callerid) => {
          vox.dialed = number;
          vox.callerid = callerid;
          setTimeout(() => pstn.emit(V.CallEvents.Ringing), 150);
          setTimeout(() => pstn.emit(V.CallEvents.Connected), 400);
          return pstn;
        },
        createASR: (params) => { asr = new VCall("asr"); asr.params = params; asr.stop = () => {}; return asr; },
        terminate: () => vox.log.push("terminate"),
      },
      setTimeout: (fn, ms) => window.setTimeout(fn, Math.min(ms, 60000)),
    };
    // eslint-disable-next-line no-new-func
    new Function(...Object.keys(globals), code)(...Object.values(globals));
    alerting({ call: web, customData });
  }

  // ------------------------------------------------------------ Web SDK
  class Call extends Emitter {
    constructor(settings) {
      super();
      this.settings = settings;
      runScenario(this, settings.customData).catch((e) => { vox.log.push(`scenario error: ${e}`); this.emit(CallEvents.Failed, { code: 500, reason: String(e) }); });
    }
    sendMessage(text) { this._toCloud?.(text); }
    hangup() { this._hangupCloud?.(); }
  }

  const client = {
    state: ClientState.DISCONNECTED,
    async init(cfg) { vox.init = cfg; return {}; },
    getClientState() { return this.state; },
    async connect() { this.state = ClientState.CONNECTED; return true; },
    async requestOneTimeLoginKey(username) {
      vox.login = username;
      const err = new Error("302"); // настоящий SDK отдаёт ключ событием AuthResult с кодом 302
      Object.assign(err, { result: false, code: 302, key: "e2e-key-" + Math.random().toString(16).slice(2, 8) });
      this.key = err.key;
      throw err;
    },
    async loginWithOneTimeKey(username, hash) {
      const ok = (await (await fetch(`/__fake_vox/verify?key=${encodeURIComponent(this.key)}&hash=${encodeURIComponent(hash)}`)).json()).ok;
      if (!ok) throw Object.assign(new Error("401"), { result: false, code: 401 });
      this.state = ClientState.LOGGED_IN;
      return { result: true };
    },
    call(settings) { return new Call(settings); },
  };
  const ConnectionNode = { NODE_1: "NODE_1", NODE_2: "NODE_2", NODE_3: "NODE_3", NODE_4: "NODE_4", NODE_5: "NODE_5", NODE_11: "NODE_11" };
  window.VoxImplant = { getInstance: () => client, ClientState, CallEvents, ConnectionNode };
})();

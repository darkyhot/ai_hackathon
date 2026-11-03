// Имитация среды VoxEngine для проверки backend/voximplant/scenario.js без облака Voximplant.
// Запуск: node tests/voximplant/harness.js <имя случая>  → JSON с тем, что сценарий сделал.
// Имена событий, методов и списков голосов/профилей — как в официальных типах VoxEngine (@voximplant/voxengine-ci).
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function makeEnv() {
  const log = [];          // что сделал сценарий
  const timers = [];       // setTimeout: управляем временем сами
  const listeners = {};    // AppEvents
  let now = 0;

  class Emitter {
    constructor() { this.handlers = {}; }
    addEventListener(ev, fn) { (this.handlers[ev] ||= []).push(fn); }
    emit(ev, payload = {}) { for (const fn of this.handlers[ev] || []) fn({ name: ev, call: this, ...payload }); }
  }
  class Call extends Emitter {
    constructor(kind) { super(); this.kind = kind; }
    answer() { log.push(["answer", this.kind]); }
    reject(code) { log.push(["reject", this.kind, code]); }
    hangup() { log.push(["hangup", this.kind]); }
    sendMessage(text) { log.push(["toBrowser", JSON.parse(text)]); }
    say(text, params) { log.push(["say", text, params && params.voice]); }
    stopPlayback() { log.push(["stopPlayback"]); }
    sendMediaTo(unit) { log.push(["sendMediaTo", unit.kind]); }
  }
  class ASR extends Emitter {
    constructor(params) { super(); this.kind = "asr"; this.params = params; }
    stop() { log.push(["asrStop"]); }
  }
  const env = {
    log,
    calls: {},
    asrs: [],
    advance(ms) {
      now += ms;
      timers.sort((a, b) => a.at - b.at);
      while (timers.length && timers[0].at <= now) timers.shift().fn();
    },
  };
  const VoiceList = {
    SaluteSpeech: { ru_RU_sergey: "salute:sergey", ru_RU_natalia: "salute:natalia" },
    Yandex: { Neural: { ru_RU_alena: "yandex:alena" } },
  };
  const ASRProfileList = { SaluteSpeech: { ru_RU: "salute-asr:ru" }, Yandex: { ru_RU: "yandex-asr:ru" } };
  const globals = {
    Modules: { ASR: "asr" },
    require: (m) => log.push(["require", m]),
    Logger: { write: (m) => log.push(["log", m]) },
    AppEvents: { CallAlerting: "Application.CallAlerting" },
    CallEvents: {
      Connected: "Call.Connected", Disconnected: "Call.Disconnected", Failed: "Call.Failed", Ringing: "Call.Ringing",
      MessageReceived: "Call.MessageReceived", PlaybackFinished: "Call.PlaybackFinished",
    },
    ASREvents: { Result: "ASR.Result", ASRError: "ASR.Error", Stopped: "ASR.Stopped" },
    VoiceList,
    ASRProfileList,
    VoxEngine: {
      addEventListener: (ev, fn) => { listeners[ev] = fn; },
      callPSTN: (number, callerid) => {
        log.push(["callPSTN", number, callerid]);
        env.calls.pstn = new Call("pstn");
        return env.calls.pstn;
      },
      createASR: (params) => {
        const asr = new ASR(params);
        env.asrs.push(asr);
        log.push(["createASR", params.profile, params.singleUtterance]);
        return asr;
      },
      terminate: () => log.push(["terminate"]),
    },
    setTimeout: (fn, ms) => { timers.push({ at: now + ms, fn }); return timers.length; },
    JSON,
    String,
  };
  const code = fs.readFileSync(path.join(__dirname, "..", "..", "backend", "voximplant", "scenario.js"), "utf8");
  vm.runInNewContext(code, globals, { filename: "scenario.js" });
  env.incoming = (customData) => {
    env.calls.web = new Call("web");
    listeners["Application.CallAlerting"]({ call: env.calls.web, customData: JSON.stringify(customData) });
  };
  env.fromBrowser = (obj) => env.calls.web.emit("Call.MessageReceived", { text: JSON.stringify(obj) });
  env.toBrowser = () => log.filter((x) => x[0] === "toBrowser").map((x) => x[1]);
  return env;
}

const CFG = { to: "79991234567", callerid: "74951234567" };

const CASES = {
  // продавец взял трубку, агент говорит две реплики, продавец отвечает, кладёт трубку
  happy() {
    const e = makeEnv();
    e.incoming(CFG);
    e.fromBrowser({ type: "say", id: 1, text: "Здравствуйте! Я ИИ-ассистент покупателя." }); // до ответа — ждёт в очереди
    e.calls.pstn.emit("Call.Ringing");
    e.calls.pstn.emit("Call.Connected");
    e.fromBrowser({ type: "say", id: 2, text: "Квартира продаётся?" });
    e.calls.pstn.emit("Call.PlaybackFinished");
    e.calls.pstn.emit("Call.PlaybackFinished");
    e.asrs[0].emit("ASR.Result", { text: "Да, продаётся", confidence: 0.93 });
    e.asrs[0].emit("ASR.Result", { text: "   " }); // пустое не шлём
    e.calls.pstn.emit("Call.Disconnected");
    e.advance(1000);
    return e;
  },
  bad_number() { const e = makeEnv(); e.incoming({ to: "12345", callerid: CFG.callerid }); e.advance(1000); return e; },
  no_callerid() { const e = makeEnv(); e.incoming({ to: CFG.to }); return e; },
  same_number() { const e = makeEnv(); e.incoming({ to: "79953277135", callerid: "+7 995 327-71-35" }); return e; },
  busy() {
    const e = makeEnv();
    e.incoming(CFG);
    e.calls.pstn.emit("Call.Failed", { code: 486, reason: "Busy Here" });
    return e;
  },
  stop() {
    const e = makeEnv();
    e.incoming(CFG);
    e.calls.pstn.emit("Call.Connected");
    e.fromBrowser({ type: "say", id: 1, text: "Длинная реплика" });
    e.fromBrowser({ type: "say", id: 2, text: "Вторая" });
    e.fromBrowser({ type: "stop" });
    e.calls.pstn.emit("Call.PlaybackFinished"); // облако сообщает об остановке — лишнего «said» нет
    return e;
  },
  asr_restart() {
    const e = makeEnv();
    e.incoming(CFG);
    e.calls.pstn.emit("Call.Connected");
    e.asrs[0].emit("ASR.Stopped");
    e.advance(200);
    e.asrs[1].emit("ASR.Result", { text: "Ниже восьми восьмисот не могу" });
    return e;
  },
  voices() {
    const e = makeEnv();
    e.incoming({ ...CFG, voice: "Yandex.Neural.ru_RU_alena", asr: "Yandex.ru_RU" });
    e.calls.pstn.emit("Call.Connected");
    e.fromBrowser({ type: "say", id: 1, text: "Раз" });
    return e;
  },
  bad_voice() {
    const e = makeEnv();
    e.incoming({ ...CFG, voice: "constructor", asr: "Nope.xx" });
    e.calls.pstn.emit("Call.Connected");
    e.fromBrowser({ type: "say", id: 1, text: "Раз" });
    return e;
  },
  client_hangup() {
    const e = makeEnv();
    e.incoming(CFG);
    e.calls.pstn.emit("Call.Connected");
    e.fromBrowser({ type: "hangup" });
    e.fromBrowser({ type: "say", id: 5, text: "после конца" });
    e.advance(1000);
    return e;
  },
  timeout() {
    const e = makeEnv();
    e.incoming(CFG);
    e.calls.pstn.emit("Call.Connected");
    e.advance(10 * 60 * 1000 + 1);
    return e;
  },
  second_call() {
    const e = makeEnv();
    e.incoming(CFG);
    const first = e.calls.web;
    e.incoming(CFG);
    e.calls.web = first;
    return e;
  },
  long_text() {
    const e = makeEnv();
    e.incoming(CFG);
    e.calls.pstn.emit("Call.Connected");
    e.fromBrowser({ type: "say", id: 1, text: "а".repeat(5000) });
    e.fromBrowser({ type: "say", id: 2, text: "" });
    return e;
  },
};

const name = process.argv[2];
const e = CASES[name]();
process.stdout.write(JSON.stringify({ log: e.log, toBrowser: e.toBrowser() }));

/*
 * Сценарий VoxEngine «Агент сделки»: звонок продавцу на телефон.
 *
 * Сервер сам загружает этот файл в приложение Voximplant (python -m backend.phone setup или первый звонок).
 *
 * Как устроено:
 *   браузер (Web SDK) ──звонок «seller»──▶ этот сценарий ──callPSTN──▶ телефон продавца
 *   браузер ──sendMessage {"type":"say"}──▶ сценарий: синтез речи в линию (call.say)
 *   телефон ──звук──▶ распознавание (ASR) ──sendMessage {"type":"heard"}──▶ браузер ──▶ агент
 *
 * Звук продавца в браузер не идёт: на сайте разговор не звучит, туда приходит только текст.
 * Записи разговора нет: голос продавца нигде не сохраняется.
 *
 * Сообщения браузеру (JSON в Call.sendMessage):
 *   {"type":"ringing"} | {"type":"answered"} | {"type":"said","id":N} | {"type":"heard","text":"…"}
 *   {"type":"failed","code":486,"reason":"…"} | {"type":"ended","reason":"seller"|"client"|"timeout"|…}
 *   {"type":"error","what":"tts"|"asr","text":"…"}
 * Сообщения от браузера: {"type":"say","id":N,"text":"…"} | {"type":"stop"} | {"type":"hangup"}
 * Параметры звонка — customData при вызове: {"to":"79991234567","callerid":"7…","voice":"…","asr":"…"}
 */
require(Modules.ASR);

var MAX_CALL_MS = 10 * 60 * 1000; // разговор дольше 10 минут — обрываем, чтобы не платить за забытую линию
var MAX_TEXT = 1500;               // одна реплика агента
var DEFAULT_VOICE = "SaluteSpeech.ru_RU_sergey";
var DEFAULT_ASR = "SaluteSpeech.ru_RU";

var web = null;      // звонок из браузера
var pstn = null;     // звонок продавцу
var asr = null;
var cfg = {};
var ended = false;
var answered = false;
var speaking = null; // реплика, которая звучит сейчас
var queue = [];

function send(obj) {
  if (!web || ended && obj.type !== "ended" && obj.type !== "failed") return;
  try { web.sendMessage(JSON.stringify(obj)); } catch (e) { Logger.write("sendMessage: " + e); }
}

/** «SaluteSpeech.ru_RU_sergey» -> VoiceList.SaluteSpeech.ru_RU_sergey; неизвестное имя — голос по умолчанию. */
function pick(root, name, fallback) {
  var node = root;
  var parts = String(name || "").split(".");
  for (var i = 0; i < parts.length && node; i++) node = node[parts[i]];
  if (node && typeof node !== "function" && name) return node;
  return fallback === undefined ? null : pick(root, fallback);
}

function finish(reason) {
  if (ended) return;
  send({ type: "ended", reason: reason });
  ended = true;
  try { if (asr) asr.stop(); } catch (e) { /* уже остановлено */ }
  try { if (pstn) pstn.hangup(); } catch (e) { /* уже завершён */ }
  try { if (web) web.hangup(); } catch (e) { /* уже завершён */ }
  setTimeout(function () { VoxEngine.terminate(); }, 300);
}

function playNext() {
  if (ended || speaking || !queue.length || !pstn || !answered) return;
  speaking = queue.shift();
  pstn.say(speaking.text, { voice: pick(VoiceList, cfg.voice, DEFAULT_VOICE), progressivePlayback: true });
}

function startAsr() {
  if (ended || !pstn) return;
  asr = VoxEngine.createASR({ profile: pick(ASRProfileList, cfg.asr, DEFAULT_ASR), singleUtterance: false });
  asr.addEventListener(ASREvents.Result, function (e) {
    var text = String(e.text || "").trim();
    if (text) send({ type: "heard", text: text, confidence: e.confidence });
  });
  asr.addEventListener(ASREvents.ASRError, function (e) {
    send({ type: "error", what: "asr", text: String(e.error || e.reason || "ошибка распознавания") });
  });
  asr.addEventListener(ASREvents.Stopped, function () {
    // у провайдеров распознавания есть лимит на одну сессию — пока разговор идёт, слушаем дальше
    if (!ended && answered) setTimeout(startAsr, 100);
  });
  pstn.sendMediaTo(asr); // распознаём только голос продавца, голос агента в ASR не попадает
}

VoxEngine.addEventListener(AppEvents.CallAlerting, function (e) {
  if (web) { e.call.reject(486); return; } // одна сессия — один звонок
  web = e.call;
  try { cfg = JSON.parse(e.customData || "{}") || {}; } catch (err) { cfg = {}; }
  web.addEventListener(CallEvents.Disconnected, function () { finish("client"); });
  web.addEventListener(CallEvents.MessageReceived, function (m) {
    var msg;
    try { msg = JSON.parse(m.text); } catch (err) { return; }
    if (!msg || ended) return;
    if (msg.type === "say") {
      var text = String(msg.text || "").slice(0, MAX_TEXT).trim();
      if (!text) { send({ type: "said", id: msg.id }); return; }
      queue.push({ id: msg.id, text: text });
      playNext();
    } else if (msg.type === "stop") {
      queue = [];
      if (speaking && pstn) {
        var cut = speaking;
        speaking = null;
        pstn.stopPlayback();
        send({ type: "said", id: cut.id });
      }
    } else if (msg.type === "hangup") {
      finish("client");
    }
  });
  web.answer();

  var to = String(cfg.to || "").replace(/\D/g, "");
  var callerid = String(cfg.callerid || "").replace(/\D/g, "");
  if (!/^7\d{10}$/.test(to)) { send({ type: "failed", code: 484, reason: "Номер продавца: нужен российский номер +7 и 10 цифр." }); finish("bad_number"); return; }
  if (!callerid) { send({ type: "failed", code: 403, reason: "Нет номера, с которого звонить (арендованный номер или подтверждённый Caller ID)." }); finish("no_callerid"); return; }

  pstn = VoxEngine.callPSTN(to, callerid);
  pstn.addEventListener(CallEvents.Ringing, function () { send({ type: "ringing" }); });
  pstn.addEventListener(CallEvents.Connected, function () {
    answered = true;
    send({ type: "answered" });
    startAsr();
    playNext();
  });
  pstn.addEventListener(CallEvents.Failed, function (ev) {
    send({ type: "failed", code: ev.code, reason: String(ev.reason || "") });
    finish("failed");
  });
  pstn.addEventListener(CallEvents.Disconnected, function () { finish("seller"); });
  pstn.addEventListener(CallEvents.PlaybackFinished, function (ev) {
    var done = speaking;
    speaking = null;
    if (ev.error) send({ type: "error", what: "tts", text: String(ev.error) });
    if (done) send({ type: "said", id: done.id });
    playNext();
  });
  setTimeout(function () { finish("timeout"); }, MAX_CALL_MS);
});

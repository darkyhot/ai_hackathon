"""Звонок продавцу на телефон через Voximplant — в браузере, без облака.

  .venv/bin/python -m tests.e2e.server --port 8795 --fake-voximplant &
  .venv/bin/python -m tests.e2e.phone_flow --base http://127.0.0.1:8795 --shots /tmp/shots

Web SDK подменён tests/e2e/fake_voximplant.js: он исполняет в странице настоящий backend/voximplant/scenario.js,
а «продавец» отвечает по сценарию как результат распознавания. Проверяем весь путь: кнопка «Позвонить мне на телефон»
→ phone.js → вход по одноразовому ключу (хеш считает наш сервер) → сценарий набирает номер → реплики агента
уходят в синтез → ответы продавца доходят до агента → торг → итог звонка и карточка цены.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from playwright.async_api import async_playwright

from .browser_flow import R, chat_text, click, ok, page, send, wait_text

SELLER = ["Да, продаётся. Один собственник.", "Ниже 8 800 000 не могу.", "Хорошо, согласен", "Давайте в субботу в 12"]


async def main(base: str, shots: str) -> int:
    if shots:
        os.makedirs(shots, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        # Настоящий Web SDK (frontend/vendor/voximplant.min.js): есть всё, что вызывает phone.js
        ctx, pg = await page(b, base)
        api = await pg.evaluate("""() => new Promise((resolve, reject) => {
          delete window.VoxImplant;
          const s = document.createElement('script'); s.src = '/__fake_vox/real-sdk.js';
          s.onload = () => {
            const V = window.VoxImplant, c = V.getInstance();
            resolve({
              methods: ['init', 'connect', 'getClientState', 'requestOneTimeLoginKey', 'loginWithOneTimeKey', 'call']
                .filter((m) => typeof c[m] !== 'function'),
              state: V.ClientState && V.ClientState.LOGGED_IN !== undefined && V.ClientState.CONNECTED !== undefined,
              events: ['Connected', 'Disconnected', 'Failed', 'MessageReceived'].filter((e) => !V.CallEvents[e]),
              node: V.ConnectionNode && V.ConnectionNode.NODE_4,
            });
          };
          s.onerror = reject; document.head.append(s);
        })""")
        ok("H: настоящий Web SDK 4.15: все нужные методы и события на месте",
           not api["methods"] and api["state"] and not api["events"] and api["node"] == "NODE_4", str(api))
        await ctx.close()

        ctx, pg = await page(b, base)
        await pg.evaluate(f"localStorage.setItem('fakeVoxSeller', {json.dumps(json.dumps(SELLER, ensure_ascii=False))})")
        await pg.reload()
        health = await pg.evaluate("fetch('/api/health').then(r => r.json())")
        ok("H: сервер видит Voximplant", health["phone"]["configured"] and health["phone"]["provider"] == "voximplant")

        await send(pg, "Двушка в Казани до 9 млн, вторичка, наличными")
        await click(pg, "Подтвердить рамки")
        await pg.wait_for_selector("[data-selection=proceed]:not([disabled])", timeout=60000)
        await asyncio.sleep(1)
        await pg.locator('[data-vote="up"]').first.click()
        await pg.click("[data-selection=proceed]")
        await pg.wait_for_selector("[data-phone]", timeout=60000)
        ok("H: на карточке звонка есть «Позвонить мне на телефон»",
           await pg.get_by_role("button", name="Позвонить мне на телефон").count() == 1)
        await pg.fill("[data-phone]", "8 (999) 123-45-67")
        await click(pg, "Позвонить мне на телефон")

        ok("H: звонок завершился и агент подвёл итог", await wait_text(pg, "Узнал у продавца", 120))
        vox = await pg.evaluate("window.__vox")
        ok("H: вход в Voximplant по одноразовому ключу (хеш от сервера принят)",
           vox["login"] == "agent@domclick-agent.e2e.n4.voximplant.com" and vox["init"]["micRequired"] is False)
        ok("H: Web SDK подключается к узлу аккаунта (российские узлы — свой балансировщик)", vox["init"].get("node") == "NODE_4",
           str(vox["init"].get("node")))
        ok("H: сценарий набрал введённый номер с номера аккаунта", (vox["dialed"], vox["callerid"]) == ("79991234567", "74950000000"),
           f"{vox['dialed']} с {vox['callerid']}")
        said = [s["text"] for s in vox["said"]]
        ok("H: агент представился голосом Voximplant", any("ИИ-ассистент" in t for t in said) and vox["said"][0]["voice"] == "salute:sergey",
           said[0][:80] if said else "ничего не сказано")
        ok("H: продавец отвечал, агент слышал его", len(vox["heard"]) >= 2, f"{len(vox['heard'])} ответов")
        text = await chat_text(pg)
        call_text = await pg.locator(".call, #stage").first.inner_text() if await pg.locator(".call, #stage").count() else ""
        ok("H: ответы продавца в расшифровке звонка", "Ниже 8 800 000" in call_text + text)
        ok("H: агент сказал «Так, понял» пока думал", any(t in ("Так, понял.", "Угу, секунду.", "Понял вас.", "Хорошо, секунду.") for t in said))
        ok("H: распознаётся только голос продавца", "sendMediaTo asr" in vox["log"])
        ok("H: звонок закрыт в облаке", "terminate" in vox["log"] or "pstn hangup" in vox["log"], ", ".join(vox["log"][-4:]))
        ok("H: дальше — подтверждение цены клиентом", await pg.get_by_role("button", name="Подтвердить").count() >= 1
           or "Подтвердить" in text)
        ok("H: без ошибок в консоли", not pg.errs, "; ".join(pg.errs[:3]))
        if shots:
            await pg.screenshot(path=f"{shots}/H_phone_call.png")
        await ctx.close()

        # Номер с ошибкой: звонок не уходит, кнопки снова доступны
        ctx, pg = await page(b, base)
        await send(pg, "Двушка в Казани до 9 млн, вторичка, наличными")
        await click(pg, "Подтвердить рамки")
        await pg.wait_for_selector("[data-selection=proceed]:not([disabled])", timeout=60000)
        await asyncio.sleep(1)
        await pg.locator('[data-vote="up"]').first.click()
        await pg.click("[data-selection=proceed]")
        await pg.wait_for_selector("[data-phone]", timeout=60000)
        await pg.fill("[data-phone]", "12345")
        await click(pg, "Позвонить мне на телефон")
        await asyncio.sleep(1)
        note = await pg.locator(".dial-note").inner_text() if await pg.locator(".dial-note").count() else ""
        ok("H: неверный номер — понятная ошибка без звонка", "российский номер" in note, note)
        ok("H: можно выбрать другой режим", await pg.get_by_role("button", name="Звонить — продавца играет ИИ").is_enabled())
        await ctx.close()
        await b.close()
    print(f"ИТОГО: {sum(R)}/{len(R)}")
    return 0 if all(R) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8795/")
    ap.add_argument("--shots", default="")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.base.rstrip("/") + "/", a.shots)))

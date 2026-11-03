"""Оболочка, карта и порядок запуска аудио без настоящих звонков.

Запуск против tests.e2e.server: python -m tests.e2e.ui_regressions --base http://127.0.0.1:8791
Аудио проверяется на поддельном AudioContext; тест не звонит и не доказывает работу iOS в фоне.
"""
import argparse
import asyncio
import base64

from playwright.async_api import async_playwright

PIXEL = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jFz8AAAAASUVORK5CYII=')


async def main(base):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={'width': 390, 'height': 844})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        await page.route('https://tile.openstreetmap.org/**', lambda route: route.fulfill(body=PIXEL, content_type='image/png'))
        await page.goto(base, wait_until='domcontentloaded')
        await page.locator('.cookie-note button').click()
        assert await page.locator('#aggregatorHome').is_visible()
        await page.locator('[data-market=secondary]').click()
        await page.fill('#portalCity', 'Казань')
        await page.fill('#portalBudget', '9')
        await page.route('**/api/session', lambda route: route.fulfill(status=503, json={'detail': 'Тест передачи формы'}))
        async with page.expect_request('**/api/session') as sent:
            await page.locator('[data-action=portal-search]').click()
        query = (await sent.value).post_data_json['query']
        assert 'Казань' in query and '9 млн' in query and 'только вторичка' in query, query
        await page.wait_for_function('query => document.getElementById("query").value === query', arg=query)
        assert await page.locator('#sendBtn').is_enabled()
        await page.unroute('**/api/session')
        await page.goto(base, wait_until='domcontentloaded')
        await page.locator('.agent-launcher').click()
        await page.fill('#entryQuery', 'Двушка в Казани до 9 млн, наличными')
        await page.locator('#entryForm button[type=submit]').click()
        await page.get_by_role('button', name='Подтвердить рамки', exact=True).last.click(timeout=30_000)
        await page.wait_for_selector('[data-selection=proceed]:not([disabled])', timeout=30_000)
        await page.wait_for_selector('.leaflet-tile-loaded', timeout=15_000)
        assert not await page.locator('#chatPanel').evaluate("node => node.classList.contains('expanded')")
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert 'CARTO' not in await page.locator('.leaflet-control-attribution').inner_text()
        await page.screenshot(path='/tmp/ui-mobile-selection.png', full_page=True)
        # Обычный текстовый вопрос модели должен снова открыть свёрнутый мобильный чат.
        await page.evaluate("""async () => {
          const {state}=await import('/js/state.js');
          state.es.onmessage(new MessageEvent('message',{data:JSON.stringify({
            type:'message',role:'agent',text:'Продолжать поиск в этом бюджете?',await_reply:true
          })}));
        }""")
        await page.wait_for_selector('#chatPanel.expanded')
        await page.locator('#chatToggle').click()
        # Очередь модели видна и на телефоне; завершение ожидания убирает индикатор.
        await page.evaluate("""async () => {
          const {state}=await import('/js/state.js');
          state.es.onmessage(new MessageEvent('message',{data:JSON.stringify({type:'model_wait',on:true})}));
        }""")
        await page.wait_for_selector('#chatPanel.expanded')
        await page.wait_for_function("document.querySelector('.msg-thinking')?.textContent.includes('жду очередь')")
        await page.evaluate("""async () => {
          const {state}=await import('/js/state.js');
          state.es.onmessage(new MessageEvent('message',{data:JSON.stringify({type:'model_wait',on:false})}));
        }""")
        await page.wait_for_selector('.msg-thinking', state='detached')
        await page.locator('#chatToggle').click()
        print('PASS: новая главная → реальный запрос → подборка, мобильная ширина и локальный Leaflet')

        # Полная ошибка плиток должна давать ссылку на карту вместо немого пустого прямоугольника.
        await page.unroute('https://tile.openstreetmap.org/**')
        await page.route('https://tile.openstreetmap.org/**', lambda route: route.abort())
        await page.evaluate("""async () => {
          const {mountMap}=await import('/js/map.js');
          const node=document.createElement('div');node.className='map';node.id='failure-map';
          document.getElementById('stage').append(node);
          await mountMap(node,[{id:'test',lat:55.796,lng:49.106,price:9000000,area:50,rooms:2,address:'Тестовый адрес'}]);
        }""")
        await page.wait_for_selector('#failure-map .map-error a', timeout=10_000)
        print('PASS: ошибка подложки видна пользователю, есть ссылка на внешнюю карту')

        # 2ГИС через локальный endpoint: сохранены маркеры, ключ в браузер не передаётся.
        await page.route('**/api/map', lambda route: route.fulfill(json={'provider': '2gis', 'tiles': '/api/map/tiles/{z}/{x}/{y}.png'}))
        await page.route('**/api/map/tiles/**', lambda route: route.fulfill(body=PIXEL, content_type='image/png'))
        await page.evaluate("""async () => {
          const {mountMap,unmountMap}=await import('/js/map.js');
          unmountMap(document.getElementById('failure-map'));
          document.getElementById('failure-map').remove();
          const node=document.createElement('div');node.className='map';node.id='twogis-map';
          document.getElementById('stage').append(node);
          await mountMap(node,[{id:'test',lat:55.796,lng:49.106,price:9000000,area:50,rooms:2,address:'Тест'}]);
        }""")
        await page.wait_for_selector('#twogis-map .leaflet-tile-loaded')
        assert '2ГИС' in await page.locator('#twogis-map .leaflet-control-attribution').inner_text()
        assert await page.locator('#twogis-map .flat-pin').count() == 1
        await page.unroute('**/api/map/tiles/**')
        await page.route('**/api/map/tiles/**', lambda route: route.abort())
        await page.unroute('https://tile.openstreetmap.org/**')
        await page.route('https://tile.openstreetmap.org/**', lambda route: route.fulfill(body=PIXEL, content_type='image/png'))
        await page.evaluate("document.querySelector('#twogis-map .leaflet-control-zoom-in').click()")
        await page.wait_for_selector('#twogis-map .map-error')
        await page.wait_for_function("document.querySelector('#twogis-map .leaflet-control-attribution').textContent.includes('OpenStreetMap')")
        assert await page.locator('#twogis-map .flat-pin').count() == 1
        print('PASS: 2ГИС с ценами квартир; при отказе сервиса подложка меняется на OSM')

        result = await page.evaluate("""async () => {
          const {PhoneCall}=await import('/js/phone.js');
          const {capturePhrase,prepareRecordingAudio}=await import('/js/voice.js');
          const original=window.AudioContext;
          const originalMedia=navigator.mediaDevices?.getUserMedia;
          const events=[];
          class FakeContext extends EventTarget {
            constructor(){ super();this.state='suspended';this.sampleRate=48000;events.push('create'); }
            resume(){events.push('resume');this.state='running';this.onstatechange?.();this.dispatchEvent(new Event('statechange'));return Promise.resolve();}
            createMediaStreamDestination(){return {stream:{kind:'outgoing'}};}
            createBuffer(){return {};}
            createAnalyser(){return {fftSize:1024,getFloatTimeDomainData(buf){buf.fill(0);},disconnect(){events.push('analyser-disconnect');}};}
            createMediaStreamSource(){return {connect(){},disconnect(){events.push('source-disconnect');}};}
            createBufferSource(){return {connect(){},start(){queueMicrotask(()=>this.onended?.());},stop(){this.onended?.();}};}
            decodeAudioData(){events.push('decode');return Promise.resolve({});}
            close(){this.state='closed';events.push('close');return Promise.resolve();}
          }
          window.AudioContext=FakeContext;
          if(navigator.mediaDevices)navigator.mediaDevices.getUserMedia=async()=>{events.push('mic');throw new Error('no mic in test');};
          // Звонок через Voximplant: говорит и слушает облако — браузер не создаёт звук и не просит микрофон
          const phone=new PhoneCall();
          await phone.unlockAudio();
          const sent=[];
          let handlers={};
          phone.connect=async()=>{events.push('connect');return {cfg:{callerid:'74950000000',voice:'v',asr:'a'},c:{call:(settings)=>{events.push('call');return {on:(ev,fn)=>{handlers[ev]=fn;},sendMessage:(t)=>{sent.push(JSON.parse(t));},hangup(){events.push('hangup');}};}}};};
          window.VoxImplant={CallEvents:{Connected:'Connected',Disconnected:'Disconnected',Failed:'Failed',MessageReceived:'MessageReceived'}};
          const dial=phone.dial('79990000000');
          await new Promise(r=>setTimeout(r,20));
          handlers.MessageReceived({text:JSON.stringify({type:'answered'})});
          await dial;
          const speaking=phone.say('Тест');
          if(sent.at(-1).type!=='say'||sent.at(-1).text!=='Тест')throw new Error('реплика не ушла в облако');
          handlers.MessageReceived({text:JSON.stringify({type:'said',id:sent.at(-1).id})});
          await speaking;
          handlers.MessageReceived({text:JSON.stringify({type:'heard',text:'Да, продаётся'})});
          const heard=await phone.listen();
          if(heard.text!=='Да, продаётся')throw new Error('ответ продавца не дошёл: '+JSON.stringify(heard));
          phone.hangup();
          if(sent.at(-1).type!=='hangup')throw new Error('облако не узнало о конце звонка');
          if(events.includes('create')||events.includes('mic'))throw new Error('звонок трогает звук/микрофон браузера: '+events);
          // Микрофон для голосового режима (voice.js): запись в Safari (AAC), узлы освобождаются
          const originalRecorder=window.MediaRecorder;
          window.MediaRecorder=class {
            static isTypeSupported(mime){return mime.startsWith('audio/mp4');}
            constructor(stream,options){this.mimeType=options.mimeType;}
            start(){this.state='recording';}
            stop(){this.state='inactive';queueMicrotask(()=>this.onstop());}
          };
          const microphone=prepareRecordingAudio();
          const recording=capturePhrase({}, {audioContext:microphone.context});
          recording.stop();const recorded=await recording.result;
          if(!recorded.blob.type.startsWith('audio/mp4'))throw new Error('Safari AAC recording not supported');
          if(!events.includes('source-disconnect'))throw new Error('capture nodes leaked');
          const phrase=capturePhrase({}, {audioContext:microphone.context});
          phrase.stop();await phrase.result;
          if(microphone.context.state!=='running')throw new Error('microphone context closed between phrases');
          microphone.release();
          if(microphone.context.state!=='closed')throw new Error('microphone context not released');
          window.MediaRecorder=originalRecorder;
          window.AudioContext=original;
          if(navigator.mediaDevices)navigator.mediaDevices.getUserMedia=originalMedia;
          return events;
        }""")
        print('PASS: звонок без звука и микрофона в браузере; микрофон голосового режима:', result)
        assert not errors, errors
        await browser.close()
        print('ИТОГО: 4/4 группы проверок')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:8791')
    asyncio.run(main(parser.parse_args().base))

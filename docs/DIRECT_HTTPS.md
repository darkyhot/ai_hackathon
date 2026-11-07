# Прямой HTTPS-вход

Подготовлен отдельный вход `HTTPS → Nginx → 127.0.0.1:8000` для сетей, в которых
загрузка через Cloudflare зависает. Основное приложение, БД и провайдеры не меняются.
Существующий Cloudflare Tunnel продолжает работать. Пока DNS указывает на Cloudflare,
обычный браузер использует прежний вход; прямой проверяется через `curl --resolve`.

## Настройка сервера

Конфигурация: [domclick-agent.conf](../deploy/nginx/domclick-agent.conf).
Она сохраняет SSE без буферизации, передачу WebSocket Upgrade и HTTPS-заголовки.
Лимит тела 6 MiB пропускает допустимые приложением записи речи до 5 MiB.

На рабочем сервере установлен Nginx из репозитория Ubuntu и Certbot. Сертификат
Let's Encrypt получен заранее через webroot работающего приложения, без смены DNS:

```sh
sudo certbot certonly --webroot \
  --webroot-path /home/admin/hackaton_progect/frontend \
  --domain domclick-agent.cikada-tech.com \
  --cert-name domclick-agent.cikada-tech.com \
  --non-interactive --agree-tos --register-unsafely-without-email --key-type ecdsa
```

FastAPI раздаёт `frontend/.well-known/acme-challenge/`. Проверка сертификата проходит
через текущий Tunnel, а после переключения DNS — через Nginx в то же приложение.
Закрытый ключ хранится только в `/etc/letsencrypt`; в Git его нет.

Файл Nginx установлен как `/etc/nginx/sites-available/mesto`, включён ссылкой
из `sites-enabled`; стандартный сайт Ubuntu отключён. Перед запуском выполнен
`sudo nginx -t`. В `/etc/letsencrypt/renewal-hooks/deploy/mesto-nginx` установлен
[обработчик продления](../deploy/nginx/renew-certificate.sh): проверка конфигурации
и reload Nginx после успешного обновления. Включён `certbot.timer`.

## Переключение DNS

В Cloudflare для зоны `cikada-tech.com` заменить запись `domclick-agent`, которая
ведёт в Tunnel, на следующую (CNAME и A с одним именем одновременно не оставлять):

| Параметр | Значение |
| --- | --- |
| Тип | A |
| Имя | domclick-agent |
| IPv4 | 85.208.85.70 |
| Proxy status | DNS only, серое облако |
| TTL | Auto |

Домен остаётся прежним. Автоматические адреса Cloudflare исчезнут после отключения
проксирования; отдельной AAAA-записи для этого хоста быть не должно, пока IPv6-вход
не настроен и не проверен. Для возврата используется сохранённое прежнее значение
CNAME с включённым проксированием; работающий Tunnel удалять не требуется.

Смена записи выполняется владельцем DNS: у приложения и серверного Tunnel нет
доступа к редактированию зоны. Это последний шаг, до него прямой вход уже проверяется:

```sh
curl --resolve domclick-agent.cikada-tech.com:443:85.208.85.70 \
  https://domclick-agent.cikada-tech.com/api/health/ready
sudo certbot renew --cert-name domclick-agent.cikada-tech.com --dry-run
```

После смены DNS проверить резолвинг, полную загрузку страницы и JavaScript,
авторизацию и доставку SSE. Доступ через проблемного провайдера дополнительно
проверяет пользователь без VPN; тест датацентра не заменяет проверку сети МТС.

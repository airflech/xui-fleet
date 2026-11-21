# Файл fleet.yaml и консоль

Всё, что настраивается в интерфейсе, хранится в файле `fleet.yaml` рядом с программой. Интерфейс сам его правит;
вручную его редактировать не обязательно, но можно — например, чтобы задать шаблон подключений для всех серверов.
Секреты (пароли SSH, ключи панелей) в `fleet.yaml` не попадают — они в `.xui-fleet/state.json`.

## Пример

```yaml
version: 1
panel:
  version: v3.11.0          # какую версию 3x-ui ставить

master:
  name: ru
  host: 203.0.113.10
  label: RU
  tls:
    mode: domain            # domain | ip | selfsigned
    domain: sub.example.com
    reserve_domains: [reserve.example.com]
    address_mode: both      # подключения «через RU» по обоим доменам
  cascades: true            # роль «каскады»
  vpn: false                # роль «сам VPN-сервер»
  warp: true                # «RU · WARP»

defaults:
  set:                      # шаблон подключений для всех зарубежных серверов
    xhttp: true
    hysteria2: true
    cascade_xhttp: true
    cascade_hysteria2: true
    cascade_tcp: false
    warp: true
    cascade_warp: true

nodes:
  - name: uk-1
    host: 198.51.100.20
    label: 🇬🇧 UK
  - name: de-1
    host: 198.51.100.30
    label: Germany
    hide: [direct]          # не показывать прямой вход в подписке

clients:
  allowed_apps: [happ, incy]
  vpn_name: Мой VPN
  support_url: https://t.me/my_support

routing:
  ru_direct_on_master: true
  client_profile: true
```

После ручной правки нажмите «Исправить» в интерфейсе или выполните `xui-fleet apply`.

## Консоль

Всё доступно и командами — из папки программы: `.venv/bin/xui-fleet --help` (на Windows — `.venv\Scripts\xui-fleet`).

| Команда | Что делает |
| --- | --- |
| `apply` | Приводит серверы и панель к `fleet.yaml`. Можно запускать сколько угодно раз. |
| `plan` | Показывает, что изменит `apply`, ничего не меняя. |
| `node add NAME HOST` | Записывает сервер в конфиг и настраивает только его. |
| `node remove NAME` | Убирает сервер из сети и конфига (сам сервер не трогает). |
| `node list`, `status` | Серверы из конфига / живое состояние с главного сервера. |
| `user add NAME`, `user list` | Создать пользователя / ссылки подписок. |
| `upgrade` | Обновить 3x-ui: сначала зарубежные серверы, потом главный. |
| `backup` | Бэкап главного сервера. |
| `migrate-master НОВЫЙ_IP` | Перенос главного сервера. |
| `import-backup ФАЙЛ.db --node ИМЯ` | Перенос со старой панели 3x-ui. |
| `app-export ФАЙЛ.xfleet`, `app-import ФАЙЛ.xfleet` | Копия приложения. |
| `hide-legacy --node ИМЯ --keep тег` | Скрыть из подписки старые подключения сервера. |
| `forget-host NAME` | Забыть SSH-ключ сервера после его переустановки. |
| `ui` | Открыть интерфейс. |

## Что важно знать

- **Служебные пользователи** `fleet-bridge` (через него главный сервер ходит на зарубежные) и `fleet-check`
  (проверка подключений) — не удаляйте их.
- **Существующая панель** не переустанавливается: приложение добавляет только свои объекты (теги `fleet-*`) и не трогает
  ваши ручные настройки.
- **Пользователи**, добавленные руками в панели 3x-ui, тоже получают все новые серверы, если они есть хотя бы на одном
  подключении приложения.

Устройство приложения изнутри — [Архитектура](../ARCHITECTURE.md).

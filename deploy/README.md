# Развёртывание на Ubuntu VPS

Нейроагент рассчитан на **постоянно работающий VPS**: Telegram Business и
локальный SyntX-bridge (Playwright + Chromium) должны быть онлайн 24/7.
GitHub Actions, Render free tier и Cursor Cloud **не гарантируют** always-on —
для production нужен свой сервер.

Телефон в беззвучном режиме или с выключенными уведомлениями Telegram **нельзя**
обойти программно: критические алерты дойдут только через настроенные каналы
(Telegram, опционально SMS/push).

## Быстрая установка

```bash
sudo REPO_URL=https://github.com/YOU/reels-bot.git bash deploy/install.sh
# или скопируйте репозиторий в /opt/telegram-business-bot и:
sudo bash deploy/install.sh
```

Скрипт создаёт пользователя `tgbot`, venv, ставит зависимости, Playwright
Chromium и включает systemd-юниты.

## Первый вход в SyntX (headed)

Bridge по умолчанию запускает Chromium **не в headless** для сохранения сессии.

1. На VPS установите X11 forwarding или VNC, либо временно запустите bridge
   вручную с дисплеем:
   ```bash
   sudo -u tgbot bash -lc 'cd /opt/telegram-business-bot && source venv/bin/activate && SYNTX_HEADLESS=false python -m integrations.syntx.bridge'
   ```
2. Откройте браузер, войдите в [syntx.ai](https://syntx.ai), выберите модель
   **Claude 4.8 Opus**.
3. Убедитесь, что профиль сохранён в `SYNTX_BROWSER_PROFILE_DIR` (по умолчанию
   `./syntx_browser_profile`).
4. Остановите ручной процесс и запустите systemd:
   ```bash
   sudo systemctl start syntx-bridge
   curl -sf http://127.0.0.1:8000/health | jq .
   ```

После успешного логина можно включить `SYNTX_HEADLESS=true` в `.env`, если
сессия стабильно подхватывается из профиля (проверьте на тестовом запросе).

## Повторная авторизация

Если `session_ok: false` в `/health`:

1. `sudo systemctl stop syntx-bridge telegram-business-bot`
2. Повторите headed-логин или обновите cookies в профиле браузера.
3. `sudo systemctl start syntx-bridge` → проверьте health → запустите бота.

## Health check

```bash
curl -sf http://127.0.0.1:8000/health
```

Ожидается JSON с `ok: true` и `session_ok: true` перед включением автоответов.

Бот также поднимает свой health на порту из `PORT` (см. `env.example`).

## Логи и logrotate

Логи systemd пишутся в `/opt/telegram-business-bot/logs/`.

Пример `/etc/logrotate.d/telegram-business-bot`:

```
/opt/telegram-business-bot/logs/*.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

## Резервное копирование БД

**SQLite** (`DATABASE_URL=sqlite:///data/bot.db`):

```bash
sqlite3 /opt/telegram-business-bot/data/bot.db ".backup '/backup/bot-$(date +%F).db'"
```

**PostgreSQL**:

```bash
pg_dump "$DATABASE_URL" -Fc -f "/backup/bot-$(date +%F).dump"
```

Копируйте также `knowledge/`, `data/contacts/` и каталог профиля SyntX —
без них восстановление контекста будет неполным.

## Управление сервисами

```bash
sudo systemctl status syntx-bridge telegram-business-bot
sudo systemctl restart syntx-bridge
sudo systemctl restart telegram-business-bot
sudo journalctl -u syntx-bridge -f
```

## Честные ограничения

- SMS сработает только при заполненных `SMS_PROVIDER` и `SMS_API_KEY`; без ключей
  остаётся Telegram.
- Push webhook — опционально, нужна ваша интеграция на стороне получателя.
- Селекторы SyntX UI могут меняться — см. `integrations/syntx/config.py`.
- Полный end-to-end с Claude 4.8 Opus через bridge **ещё требует проверки** на
  вашем VPS и аккаунте SyntX.

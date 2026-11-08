"""Запуск: python -m hub. Настройки читаются из переменных окружения или файла hub/.env."""
import logging
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from .server import make_handler
from .service import Hub
from .store import Store
from .telegram_api import TelegramAPI, TelegramError

log = logging.getLogger("hub")
POLL_TIMEOUT = 25


def _load_env():
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(__file__).resolve().parent / ".env")


def _poll(hub, store):
    offset = int(store.get_meta("offset", "0") or 0)
    while True:
        try:
            updates = hub.api.call("getUpdates", http_timeout=POLL_TIMEOUT + 15, offset=offset,
                                   timeout=POLL_TIMEOUT, allowed_updates=["message", "callback_query"]) or []
        except TelegramError as exc:
            log.warning("getUpdates: %s", exc.code)
            time.sleep(5)
            continue
        for update in updates:
            offset = max(offset, update["update_id"] + 1)
            hub.process_update(update)
        if updates:
            store.set_meta("offset", offset)


def _scheduler(hub):
    while True:
        time.sleep(60)
        try:
            hub.morning_tick()
        except Exception:
            log.exception("morning_tick")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _load_env()
    token = os.environ.get("HUB_BOT_TOKEN", "").strip()
    registration = os.environ.get("HUB_REGISTRATION_TOKEN", "").strip()
    if not token or not registration:
        sys.exit("Задайте HUB_BOT_TOKEN (токен бота от @BotFather) и HUB_REGISTRATION_TOKEN (общий секрет касс).")
    api = TelegramAPI(token)
    username = os.environ.get("HUB_BOT_USERNAME", "").strip().lstrip("@") or api.call("getMe")["username"]
    store = Store(os.environ.get("HUB_DB") or str(Path(__file__).resolve().parent / "hub.sqlite3"))
    hub = Hub(store, api, username, registration, time.time)
    # Вебхук мешает getUpdates, поэтому сбрасываем его на случай старых настроек
    try:
        api.call("deleteWebhook")
    except TelegramError:
        pass
    hub.setup_commands()

    host, port = os.environ.get("HUB_HOST", "127.0.0.1"), int(os.environ.get("HUB_PORT", "8099"))
    server = ThreadingHTTPServer((host, port), make_handler(hub))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=_scheduler, args=(hub,), daemon=True).start()
    log.info("Бот @%s запущен, API на %s:%s", username, host, port)
    _poll(hub, store)


if __name__ == "__main__":
    main()

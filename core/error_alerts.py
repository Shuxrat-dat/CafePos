"""Ошибки сервера (500) приходят владельцу в Telegram: о поломке он узнаёт раньше, чем пожалуются сотрудники.

Обработчик подключается в LOGGING (config/settings.py) к журналу django.request. Журналы Django
настраиваются раньше, чем загружаются модели, поэтому core.telegram импортируется только при отправке.
"""
import logging
import threading
import time

PAUSE_SECONDS = 600  # не чаще одного сообщения в 10 минут: при поломке ошибки идут сериями


class TelegramErrorHandler(logging.Handler):
    _last_sent = 0.0
    _lock = threading.Lock()

    def emit(self, record):
        try:
            with self._lock:
                now = time.monotonic()
                if TelegramErrorHandler._last_sent and now - TelegramErrorHandler._last_sent < PAUSE_SECONDS:
                    return
                TelegramErrorHandler._last_sent = now
            request = getattr(record, "request", None)
            path = getattr(request, "path", "") if request is not None else ""
            error = type(record.exc_info[1]).__name__ if record.exc_info and record.exc_info[1] else ""
            from core import telegram

            telegram.notify_server_error(path, error)
        except Exception:  # обработчик журнала не должен ломать ответ
            pass

"""Тонкая обёртка над Bot API. Токен в тексты ошибок не попадает."""
import json
import urllib.error
import urllib.request
import uuid


class TelegramError(Exception):
    def __init__(self, code, description=""):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description

    @property
    def chat_gone(self):
        """Бот заблокирован, исключён из группы или чата нет: слать туда больше нечего."""
        return self.code == 403 or (self.code == 400 and "chat not found" in self.description.lower())


class TelegramAPI:
    def __init__(self, token, timeout=15):
        self._token = token
        self.timeout = timeout

    def _request(self, method, data, headers, timeout=None):
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                body = json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode())
            except (ValueError, OSError):
                body = {}
            raise TelegramError(exc.code, body.get("description", "")) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TelegramError(0, type(exc).__name__) from None
        if not body.get("ok"):
            raise TelegramError(body.get("error_code", 0), body.get("description", ""))
        return body.get("result")

    def call(self, method, http_timeout=None, **params):
        """http_timeout: сколько ждать ответа сервера. Параметр Telegram `timeout` (долгий опрос) идёт в params."""
        return self._request(method, json.dumps(params).encode(), {"Content-Type": "application/json"}, http_timeout)

    def send_message(self, chat_id, text, reply_markup=None):
        params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup:
            params["reply_markup"] = reply_markup
        return self.call("sendMessage", **params)

    def send_document(self, chat_id, filename, content, caption=""):
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in (("chat_id", str(chat_id)), ("caption", caption)):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        safe = filename.replace('"', "").replace("\r", "").replace("\n", "")
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{safe}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode() + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return self._request("sendDocument", b"".join(parts),
                             {"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=120)

"""Логика бота: подключение владельцев, доставка событий, ответы на команды, утренние сводки."""
import hmac
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import texts
from .telegram_api import TelegramError
from .texts import T

log = logging.getLogger("hub")

DEFAULT_TZ = "Asia/Tashkent"
MORNING_HOUR = 9
MORNING_UNTIL = 12   # позже утреннюю сводку не шлём: она уже не «утренняя»
SNAPSHOT_MAX_AGE = 36 * 3600


def _zone(name):
    """Часовой пояс заведения. Без базы tzdata (бывает на Windows) узбекские пояса считаем как UTC+5: летнего времени нет."""
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:
        return timezone(timedelta(hours=5 if (name or DEFAULT_TZ).startswith("Asia/") else 0))


class HubError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _norm_lang(value):
    return value if value in texts.LANGS else texts.DEFAULT_LANG


class Hub:
    def __init__(self, store, api, bot_username, registration_token, clock):
        self.store = store
        self.api = api
        self.bot_username = bot_username
        self.registration_token = registration_token
        self.clock = clock

    # ---------- API для касс ----------

    def link_url(self, code):
        return f"https://t.me/{self.bot_username}?start={code}"

    def register(self, name, lang, token):
        if not self.registration_token or not hmac.compare_digest(str(token or ""), self.registration_token):
            raise HubError(403, "bad registration token")
        name = (name or "").strip() or "CafePOS"
        venue_id, key = self.store.create_venue(name, _norm_lang(lang), self.clock())
        code = self.store.new_link(venue_id, self.clock())
        return {"venue_key": key, "code": code, "link_url": self.link_url(code), "bot": self.bot_username}

    def status(self, venue, want_link=False):
        chats = self.store.chats_of_venue(venue["id"])
        result = {
            "connected": bool(chats),
            "recipients": [{"chat_id": c["chat_id"], "title": c["title"]} for c in chats],
            "bot": self.bot_username,
            "has_data": bool(venue.get("snapshot")),
        }
        if want_link:
            code = self.store.active_link(venue["id"], self.clock()) or self.store.new_link(venue["id"], self.clock())
            result["link_url"] = self.link_url(code)
        return result

    def new_link(self, venue):
        code = self.store.new_link(venue["id"], self.clock())
        return {"code": code, "link_url": self.link_url(code)}

    def update_venue(self, venue, name, lang):
        self.store.rename_venue(venue["id"], (name or venue["name"]).strip() or venue["name"], _norm_lang(lang))

    def disconnect(self, venue, chat_id=None):
        if chat_id:
            self.store.remove_chat(chat_id, venue["id"])
        else:
            self.store.remove_all_chats(venue["id"])

    def send_event(self, venue, category, text):
        if category not in texts.EVENT_CATEGORIES:
            raise HubError(400, "unknown event")
        return {"sent": self.deliver(venue["id"], category, str(text)[:3500])}

    def send_test(self, venue):
        sent = 0
        for chat in self.store.chats_of_venue(venue["id"]):
            sent += self._send(chat, T(chat["lang"], "test", venue=venue["name"]))
        return {"sent": sent}

    def send_document(self, venue, filename, content, caption):
        sent = 0
        for chat in self.store.chats_of_venue(venue["id"]):
            if "backup" in chat["off"]:
                continue
            try:
                self.api.send_document(chat["chat_id"], filename, content, caption[:900])
                sent += 1
            except TelegramError as exc:
                self._on_error(chat, exc)
        return {"sent": sent}

    def set_snapshot(self, venue, data):
        if not isinstance(data, dict) or not isinstance(data.get("today"), dict):
            raise HubError(400, "bad snapshot")
        self.store.set_snapshot(venue["id"], data, self.clock())
        if data.get("venue") and data["venue"] != venue["name"]:
            self.store.rename_venue(venue["id"], data["venue"], venue["lang"])
        return {"ok": True}

    # ---------- доставка ----------

    def deliver(self, venue_id, category, text, markup=None):
        sent = 0
        for chat in self.store.chats_of_venue(venue_id):
            if category in chat["off"]:
                continue
            sent += self._send(chat, text, markup)
        return sent

    def _send(self, chat, text, markup=None):
        try:
            self.api.send_message(chat["chat_id"], text, markup)
            return 1
        except TelegramError as exc:
            self._on_error(chat, exc)
            return 0

    def _on_error(self, chat, exc):
        if exc.chat_gone:
            log.info("Чат %s больше недоступен, убираем из рассылки", chat["chat_id"])
            self.store.remove_chat(chat["chat_id"], chat["venue_id"])
        else:
            log.warning("Telegram: сообщение не доставлено (%s)", exc.code)

    def _reply(self, chat_id, text, markup=None):
        try:
            self.api.send_message(chat_id, text, markup)
        except TelegramError as exc:
            log.warning("Telegram: ответ не отправлен (%s)", exc.code)

    # ---------- команды ----------

    COMMANDS = {
        "bugun": "today", "kecha": "yesterday", "hafta": "week", "ombor": "stock",
        "smena": "shift", "top": "top",
    }

    def process_update(self, update):
        try:
            if "callback_query" in update:
                self._callback(update["callback_query"])
            elif "message" in update:
                self._message(update["message"])
        except Exception:  # один плохой апдейт не должен останавливать бота
            log.exception("Не удалось обработать апдейт")

    def _chat_title(self, chat, sender):
        if chat.get("title"):
            return chat["title"]
        person = sender if chat.get("type") == "private" else {}
        return " ".join(p for p in (person.get("first_name"), person.get("last_name")) if p) or person.get("username", "")

    def _message(self, message):
        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        if not text.startswith("/"):
            if chat.get("type") == "private" and text:
                known = self.store.chat(chat_id)
                self._reply(chat_id, T(known["lang"] if known else texts.DEFAULT_LANG, "unknown"))
            return
        parts = text.split(maxsplit=1)
        command = parts[0][1:].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if command == "start":
            return self._start(chat, arg, message.get("from") or {})
        known = self.store.chat(chat_id)
        lang = known["lang"] if known else texts.DEFAULT_LANG
        if command in ("yordam", "help"):
            return self._reply(chat_id, T(lang, "help"), texts.main_keyboard(lang) if known else None)
        if not known:
            return self._reply(chat_id, T(lang, "not_linked"))
        if command in self.COMMANDS:
            return self._report(chat_id, self.COMMANDS[command], known)
        if command == "sozlamalar":
            return self._reply(chat_id, T(lang, "settings_title"), texts.settings_keyboard(lang, known["off"]))
        if command == "til":
            new = "ru" if lang == "uz" else "uz"
            self.store.set_lang(chat_id, new)
            return self._reply(chat_id, T(new, "lang_set"), texts.main_keyboard(new))
        self._reply(chat_id, T(lang, "unknown"))

    def _start(self, chat, code, sender):
        chat_id = chat["id"]
        known = self.store.chat(chat_id)
        lang = known["lang"] if known else texts.DEFAULT_LANG
        if not code:
            return self._reply(chat_id, T(lang, "help") if known else T(lang, "start_plain"),
                               texts.main_keyboard(lang) if known else None)
        venue_id = self.store.consume_link(code, self.clock())
        if venue_id is None:
            return self._reply(chat_id, T(lang, "link_bad"))
        venue = self.store.venue(venue_id)
        chat_lang = known["lang"] if known else venue["lang"]
        first = not self.store.chats_of_venue(venue_id)
        off = [] if first else list(texts.SENSITIVE_DEFAULT_OFF)
        if known:
            off = sorted(known["off"])  # настройки чата общие для всех его заведений
        is_new = self.store.add_chat(chat_id, venue_id, self._chat_title(chat, sender), chat_lang, off, self.clock())
        key = "welcome" if is_new else "welcome_again"
        self._reply(chat_id, T(chat_lang, key, venue=venue["name"]), texts.main_keyboard(chat_lang))

    def _callback(self, query):
        data = query.get("data") or ""
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        try:
            self.api.call("answerCallbackQuery", callback_query_id=query.get("id"))
        except TelegramError:
            pass
        known = self.store.chat(chat_id) if chat_id is not None else None
        if not known:
            return
        lang = known["lang"]
        if data.startswith("cmd:"):
            self._report(chat_id, data[4:], known)
        elif data.startswith("set:") and data[4:] in texts.CATEGORIES:
            off = set(known["off"])
            off ^= {data[4:]}
            self.store.set_off(chat_id, off)
            try:
                self.api.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message.get("message_id"),
                               reply_markup=texts.settings_keyboard(lang, off))
            except TelegramError:
                pass

    def _local_days(self, snapshot):
        """(сегодня, вчера) относительно местной даты заведения. None, если данных за день нет."""
        tz = _zone(snapshot.get("tz"))
        local = datetime.fromtimestamp(self.clock(), tz).date()
        snap_day = (snapshot.get("today") or {}).get("date")
        if snap_day == local.isoformat():
            return snapshot.get("today"), snapshot.get("yesterday")
        if snap_day == (local - timedelta(days=1)).isoformat():
            return None, snapshot.get("today")
        return None, None

    def _report(self, chat_id, kind, chat):
        lang = chat["lang"]
        venues = self.store.venues_of_chat(chat_id)
        for venue in venues:
            snapshot, _at = self.store.snapshot(venue)
            if not snapshot:
                self._reply(chat_id, T(lang, "no_data"))
                continue
            today, yesterday = self._local_days(snapshot)
            asof = texts.asof_line(snapshot, lang)
            name = venue["name"]
            if kind == "today":
                body = texts.day_report(lang, name, "today", today, asof)
            elif kind == "yesterday":
                body = texts.day_report(lang, name, "yesterday", yesterday, asof)
            elif kind == "week":
                body = texts.week_report(lang, name, snapshot.get("week"), asof)
            elif kind == "stock":
                body = texts.shopping_report(lang, name, snapshot.get("shopping") or [])
            elif kind == "shift":
                body = texts.shift_report(lang, name, snapshot.get("shift"))
            elif kind == "top":
                body = texts.top_report(lang, name, snapshot)
            else:
                return
            self._reply(chat_id, body, texts.main_keyboard(lang))

    # ---------- утренние сводки ----------

    def morning_tick(self):
        """Раз в минуту: в 09:00 по местному времени заведения шлёт вчерашний итог и список закупки."""
        for venue in self.store.all_venues():
            try:
                self._morning_for(venue)
            except Exception:
                log.exception("Утренняя сводка не отправлена (заведение %s)", venue["id"])

    def _morning_for(self, venue):
        snapshot, at = self.store.snapshot(venue)
        if not snapshot or not at or self.clock() - at > SNAPSHOT_MAX_AGE:
            return
        tz = _zone(snapshot.get("tz"))
        local = datetime.fromtimestamp(self.clock(), tz)
        if not MORNING_HOUR <= local.hour < MORNING_UNTIL:
            return
        day = local.date().isoformat()
        _today, yesterday = self._local_days(snapshot)
        if venue["morning_sent"] != day and yesterday is not None:
            self.store.mark_sent(venue["id"], "morning_sent", day)
            if yesterday.get("orders"):
                for chat in self.store.chats_of_venue(venue["id"]):
                    if "morning" in chat["off"]:
                        continue
                    lang = chat["lang"]
                    body = texts.day_report(lang, venue["name"], "yesterday", yesterday)
                    self._send(chat, T(lang, "morning") + "\n\n" + body, texts.main_keyboard(lang))
        rows = snapshot.get("shopping") or []
        if venue["shopping_sent"] != day:
            self.store.mark_sent(venue["id"], "shopping_sent", day)
            if rows:
                for chat in self.store.chats_of_venue(venue["id"]):
                    if "shopping" not in chat["off"]:
                        self._send(chat, texts.shopping_report(chat["lang"], venue["name"], rows))

    def setup_commands(self):
        """Меню команд бота (кнопка слева от поля ввода). Узбекский по умолчанию, русский для русских клиентов."""
        uz = [
            ("bugun", "Bugungi tushum va foyda"), ("kecha", "Kechagi natija"), ("hafta", "Oxirgi 7 kun"),
            ("ombor", "Nima sotib olish kerak"), ("smena", "Joriy smena"), ("top", "Eng koʻp sotilganlar"),
            ("sozlamalar", "Qaysi xabarlar kelsin"), ("yordam", "Buyruqlar roʻyxati"), ("til", "Til: UZ/RU"),
        ]
        ru = [
            ("bugun", "Выручка и прибыль сегодня"), ("kecha", "Итоги вчера"), ("hafta", "Последние 7 дней"),
            ("ombor", "Что нужно купить"), ("smena", "Текущая смена"), ("top", "Самые продаваемые"),
            ("sozlamalar", "Какие сообщения получать"), ("yordam", "Список команд"), ("til", "Язык: UZ/RU"),
        ]
        for lang_code, rows in ((None, uz), ("uz", uz), ("ru", ru)):
            params = {"commands": [{"command": c, "description": d} for c, d in rows]}
            if lang_code:
                params["language_code"] = lang_code
            try:
                self.api.call("setMyCommands", **params)
            except TelegramError as exc:
                log.warning("setMyCommands не удалось (%s)", exc.code)

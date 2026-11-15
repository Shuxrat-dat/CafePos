import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

from .server import make_handler
from .service import Hub, HubError
from .store import Store
from .telegram_api import TelegramError

REG = "reg-secret"
# 8 ноября 2026, 09:30 по Ташкенту (UTC+5)
NINE_THIRTY = datetime(2026, 11, 8, 4, 30, tzinfo=timezone.utc).timestamp()


class FakeAPI:
    def __init__(self):
        self.sent, self.docs, self.calls = [], [], []
        self.blocked = set()

    def send_message(self, chat_id, text, reply_markup=None):
        if str(chat_id) in self.blocked:
            raise TelegramError(403, "Forbidden: bot was blocked by the user")
        self.sent.append((str(chat_id), text, reply_markup))

    def send_document(self, chat_id, filename, content, caption=""):
        self.docs.append((str(chat_id), filename, content, caption))

    def call(self, method, **params):
        self.calls.append((method, params))
        return {}

    def texts_to(self, chat_id):
        return [t for c, t, _m in self.sent if c == str(chat_id)]


def day(revenue=1500000, orders=30, **extra):
    base = {"date": "2026-11-08", "revenue": revenue, "orders": orders, "avg": revenue / max(orders, 1),
            "cost": revenue * 0.4, "profit": revenue * 0.6, "has_cost": True, "cash": 900000, "card": 600000,
            "cancelled": 0, "cancelled_sum": 0, "vs_last_week": 12}
    base.update(extra)
    return base


def snapshot(**extra):
    data = {
        "venue": "Kafe Test", "tz": "Asia/Tashkent", "generated_at": "2026-11-08T09:25:00+05:00",
        "today": day(), "yesterday": day(2000000, 40, date="2026-11-07"),
        "week": {"revenue": 9000000, "orders": 150, "profit": 5000000, "prev_revenue": 8000000, "has_cost": True,
                 "best_day": {"date": "2026-11-05", "revenue": 2500000}},
        "top_today": [{"name": "Плов", "name_uz": "Osh", "qty": 12, "revenue": 480000}],
        "top_week": [{"name": "Плов", "name_uz": "Osh", "qty": 80, "revenue": 3200000}],
        "shift": {"open": True, "cashier": "Aziz", "opened_at": "08:00", "revenue": 700000, "orders": 14},
        "shopping": [{"name": "Говядина", "name_uz": "Mol goʻshti", "unit": "g", "stock": 0, "suggest": 5000,
                      "days_left": None, "level": "out", "cost": 400000}],
    }
    data.update(extra)
    return data


class HubTestCase(unittest.TestCase):
    def setUp(self):
        self.now = NINE_THIRTY
        self.api = FakeAPI()
        self.store = Store()
        self.hub = Hub(self.store, self.api, "CafeBot", REG, lambda: self.now)

    def register(self, name="Kafe Test", lang="uz"):
        reply = self.hub.register(name, lang, REG)
        return self.store.venue_by_key(reply["venue_key"]), reply

    def say(self, chat_id, text, first_name="Ali"):
        self.hub.process_update({"message": {"text": text, "chat": {"id": chat_id, "type": "private"},
                                             "from": {"first_name": first_name}}})

    def connect(self, chat_id=111):
        venue, reply = self.register()
        self.say(chat_id, f"/start {reply['code']}")
        return venue


class ConnectTests(HubTestCase):
    def test_register_needs_secret(self):
        with self.assertRaises(HubError):
            self.hub.register("X", "uz", "wrong")
        with self.assertRaises(HubError):
            Hub(self.store, self.api, "CafeBot", "", lambda: 0).register("X", "uz", "")

    def test_link_has_bot_username_and_code(self):
        _venue, reply = self.register()
        self.assertEqual(reply["link_url"], f"https://t.me/CafeBot?start={reply['code']}")

    def test_start_with_code_connects_chat_once(self):
        venue, reply = self.register()
        self.say(111, f"/start {reply['code']}")
        self.assertIn("Ulandi", self.api.texts_to(111)[-1])
        self.assertTrue(self.hub.status(venue)["connected"])
        self.say(222, f"/start {reply['code']}")  # ссылка одноразовая
        self.assertIn("eskirgan", self.api.texts_to(222)[-1])
        self.assertEqual(len(self.hub.status(venue)["recipients"]), 1)

    def test_status_gives_fresh_link_until_connected(self):
        venue, _reply = self.register()
        first = self.hub.status(venue, want_link=True)["link_url"]
        self.assertEqual(first, self.hub.status(venue, want_link=True)["link_url"])

    def test_second_recipient_gets_no_backups_by_default(self):
        venue = self.connect(111)
        second = self.hub.new_link(venue)
        self.say(222, f"/start {second['code']}")
        chats = {c["chat_id"]: c for c in self.store.chats_of_venue(venue["id"])}
        self.assertEqual(chats["111"]["off"], set())
        self.assertEqual(chats["222"]["off"], {"backup", "error"})

    def test_start_without_code_explains(self):
        self.say(5, "/start")
        self.assertIn("ulanish", self.api.texts_to(5)[-1].lower())

    def test_unlinked_chat_gets_no_data(self):
        self.say(9, "/bugun")
        self.assertIn("ulanmagan", self.api.texts_to(9)[-1])

    def test_disconnect_removes_everyone(self):
        venue = self.connect()
        self.hub.disconnect(venue)
        self.assertFalse(self.hub.status(venue)["connected"])


class DeliveryTests(HubTestCase):
    def test_event_reaches_chat(self):
        venue = self.connect()
        self.assertEqual(self.hub.send_event(venue, "cancel", "Бекор")["sent"], 1)
        self.assertEqual(self.api.texts_to(111)[-1], "Бекор")

    def test_juma_orders_event_reaches_chat_and_can_be_switched_off(self):
        venue = self.connect()
        self.assertEqual(self.hub.send_event(venue, "orders", "Yangi Juma buyurtmasi")["sent"], 1)
        self.store.set_off(111, {"orders"})
        self.assertEqual(self.hub.send_event(venue, "orders", "x")["sent"], 0)

    def test_settings_keyboard_lists_juma_orders_in_both_languages(self):
        from . import texts

        for lang in texts.LANGS:
            buttons = [row[0]["callback_data"] for row in texts.settings_keyboard(lang, set())["inline_keyboard"]]
            self.assertIn("set:orders", buttons)
            self.assertIn("Juma", texts.T(lang, "cat_orders"))

    def test_unknown_event_is_rejected(self):
        venue = self.connect()
        with self.assertRaises(HubError):
            self.hub.send_event(venue, "spam", "x")

    def test_disabled_category_is_skipped(self):
        venue = self.connect()
        self.store.set_off(111, {"cancel"})
        self.assertEqual(self.hub.send_event(venue, "cancel", "x")["sent"], 0)
        self.assertEqual(self.hub.send_event(venue, "shift", "x")["sent"], 1)

    def test_blocked_chat_is_dropped(self):
        venue = self.connect()
        self.api.blocked.add("111")
        self.hub.send_event(venue, "shift", "x")
        self.assertFalse(self.hub.status(venue)["connected"])

    def test_document_goes_to_chats_with_backup_enabled(self):
        venue = self.connect(111)
        second = self.hub.new_link(venue)
        self.say(222, f"/start {second['code']}")
        self.hub.send_document(venue, "db.dump", b"abc", "copy")
        self.assertEqual([d[0] for d in self.api.docs], ["111"])


class CommandTests(HubTestCase):
    def setUp(self):
        super().setUp()
        self.venue = self.connect()
        self.hub.set_snapshot(self.venue, snapshot())

    def last(self):
        return self.api.texts_to(111)[-1]

    def test_today_in_uzbek(self):
        self.say(111, "/bugun")
        text = self.last()
        self.assertIn("Bugun", text)
        self.assertIn("1 500 000 soʻm", text)
        self.assertIn("Foyda: 900 000 soʻm (60%)", text)
        self.assertIn("+12%", text)

    def test_yesterday_and_week(self):
        self.say(111, "/kecha")
        self.assertIn("2 000 000", self.last())
        self.say(111, "/hafta")
        self.assertIn("9 000 000", self.last())
        self.assertIn("+12%", self.last())  # 9 млн против 8 млн

    def test_stock_shows_uzbek_names_and_units(self):
        self.say(111, "/ombor")
        self.assertIn("Mol goʻshti", self.last())
        self.assertIn("5 kg", self.last())
        self.assertIn("tugagan", self.last())

    def test_shift_and_top(self):
        self.say(111, "/smena")
        self.assertIn("Aziz", self.last())
        self.say(111, "/top")
        self.assertIn("Osh", self.last())

    def test_language_switch_to_russian(self):
        self.say(111, "/til")
        self.say(111, "/bugun")
        self.assertIn("Выручка", self.last())
        self.assertIn("Плов", (self.say(111, "/top"), self.last())[1])

    def test_command_with_bot_name_suffix(self):
        self.say(111, "/bugun@CafeBot")
        self.assertIn("Tushum", self.last())

    def test_no_snapshot_yet(self):
        venue, reply = self.register("Boshqa")
        self.say(333, f"/start {reply['code']}")
        self.say(333, "/bugun")
        self.assertIn("maʼlumot", self.api.texts_to(333)[-1])

    def test_stale_snapshot_shows_no_sales_today_but_yesterday_data(self):
        self.now += 24 * 3600  # на следующий день касса ещё ничего не прислала
        self.say(111, "/bugun")
        self.assertIn("Hali savdo yoʻq", self.last())
        self.say(111, "/kecha")
        self.assertIn("1 500 000", self.last())

    def test_settings_toggle_via_button(self):
        self.say(111, "/sozlamalar")
        self.hub.process_update({"callback_query": {
            "id": "1", "data": "set:review",
            "message": {"message_id": 7, "chat": {"id": 111}}}})
        self.assertEqual(self.store.chat(111)["off"], {"review"})
        self.assertEqual(self.api.calls[-1][0], "editMessageReplyMarkup")

    def test_button_runs_command(self):
        self.hub.process_update({"callback_query": {
            "id": "1", "data": "cmd:today", "message": {"message_id": 7, "chat": {"id": 111}}}})
        self.assertIn("Tushum", self.last())

    def test_unknown_command(self):
        self.say(111, "/nimadir")
        self.assertIn("Tushunmadim", self.last())

    def test_setup_commands_registers_uzbek_menu(self):
        self.hub.setup_commands()
        names = [c["command"] for c in self.api.calls[-3][1]["commands"]]
        self.assertIn("bugun", names)
        self.assertIn("ombor", names)


class MorningTests(HubTestCase):
    def setUp(self):
        super().setUp()
        self.venue = self.connect()
        self.hub.set_snapshot(self.venue, snapshot())
        self.api.sent.clear()

    def test_morning_digest_sent_once(self):
        self.hub.morning_tick()
        texts_ = self.api.texts_to(111)
        self.assertEqual(len(texts_), 2)  # итог вчера + список закупки
        self.assertIn("Xayrli tong", texts_[0])
        self.assertIn("2 000 000", texts_[0])
        self.assertIn("Mol goʻshti", texts_[1])
        self.hub.morning_tick()
        self.assertEqual(len(self.api.texts_to(111)), 2)

    def test_not_before_nine(self):
        self.now = datetime(2026, 11, 8, 2, 30, tzinfo=timezone.utc).timestamp()  # 07:30 по Ташкенту
        self.hub.morning_tick()
        self.assertEqual(self.api.sent, [])

    def test_not_after_noon(self):
        self.now = datetime(2026, 11, 8, 9, 30, tzinfo=timezone.utc).timestamp()  # 14:30
        self.hub.morning_tick()
        self.assertEqual(self.api.sent, [])

    def test_uses_last_evening_snapshot_when_day_not_started(self):
        data = snapshot(today=day(2000000, 40, date="2026-11-07"), yesterday=day(date="2026-11-06"))
        self.hub.set_snapshot(self.venue, data)
        self.hub.morning_tick()
        self.assertIn("2 000 000", self.api.texts_to(111)[0])

    def test_respects_chat_settings(self):
        self.store.set_off(111, {"morning", "shopping"})
        self.hub.morning_tick()
        self.assertEqual(self.api.sent, [])

    def test_no_orders_yesterday_sends_no_summary(self):
        self.hub.set_snapshot(self.venue, snapshot(yesterday=day(0, 0, date="2026-11-07"), shopping=[]))
        self.hub.morning_tick()
        self.assertEqual(self.api.sent, [])


class HttpTests(HubTestCase):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.hub))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, payload=None, key=None, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if key:
            request.add_header("Authorization", f"Bearer {key}")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_full_flow(self):
        status, reg = self.call("POST", "/v1/venues", {"name": "Kafe", "lang": "uz", "registration_token": REG})
        self.assertEqual(status, 200)
        key = reg["venue_key"]
        self.assertFalse(self.call("GET", "/v1/status", key=key)[1]["connected"])
        self.say(111, f"/start {reg['code']}")
        self.assertTrue(self.call("GET", "/v1/status", key=key)[1]["connected"])
        self.assertEqual(self.call("POST", "/v1/events", {"event": "shift", "text": "Smena yopildi"}, key=key)[1],
                         {"sent": 1})
        self.assertEqual(self.call("POST", "/v1/snapshot", {"data": snapshot()}, key=key)[0], 200)
        self.assertEqual(self.call("POST", "/v1/test", {}, key=key)[1], {"sent": 1})
        status, doc = self.call("POST", "/v1/document", raw=b"data", key=key,
                                headers={"X-Filename": "db.dump", "X-Caption": "nusxa"})
        self.assertEqual((status, doc), (200, {"sent": 1}))

    def test_rejects_wrong_registration_and_keys(self):
        self.assertEqual(self.call("POST", "/v1/venues", {"registration_token": "bad"})[0], 403)
        self.assertEqual(self.call("GET", "/v1/status", key="nope")[0], 401)
        self.assertEqual(self.call("GET", "/v1/status")[0], 401)

    def test_bad_snapshot_is_rejected(self):
        _s, reg = self.call("POST", "/v1/venues", {"name": "K", "registration_token": REG})
        self.assertEqual(self.call("POST", "/v1/snapshot", {"data": "x"}, key=reg["venue_key"])[0], 400)


if __name__ == "__main__":
    unittest.main()


class NetworkTests(HubTestCase):
    """Сеть заведений: несколько точек в одном чате, общий итог командой /tarmoq."""

    def setUp(self):
        super().setUp()
        self.a, reply_a = self.register("Kafe A")
        self.say(111, f"/start {reply_a['code']}")
        self.b, reply_b = self.register("Kafe B")
        self.say(111, f"/start {reply_b['code']}")
        self.hub.set_snapshot(self.a, snapshot(venue="Kafe A"))
        self.hub.set_snapshot(self.b, snapshot(venue="Kafe B", today=day(3000000, 50), yesterday=day(1000000, 10, date="2026-11-07"),
                                               week={"revenue": 20000000, "orders": 300, "profit": 9000000,
                                                     "has_cost": True, "prev_revenue": 1}))
        self.api.sent.clear()

    def last(self):
        return self.api.texts_to(111)[-1]

    def test_total_share_and_best_point(self):
        self.say(111, "/tarmoq")
        text = self.last()
        self.assertIn("4 500 000 soʻm", text)
        self.assertLess(text.index("1. Kafe B"), text.index("2. Kafe A"))
        self.assertIn("67%", text)
        self.assertIn("33%", text)
        self.assertIn("Eng yaxshisi: Kafe B", text)
        self.assertIn("Foyda: 2 700 000", text)  # 60% от 4,5 млн

    def test_yesterday_and_week_arguments(self):
        self.say(111, "/tarmoq kecha")
        self.assertIn("3 000 000 soʻm", self.last())  # 2 000 000 + 1 000 000
        self.say(111, "/tarmoq hafta")
        self.assertIn("29 000 000 soʻm", self.last())  # 9 000 000 + 20 000 000

    def test_russian_labels(self):
        self.say(111, "/til")
        self.say(111, "/tarmoq")
        self.assertIn("Выручка сети", self.last())
        self.assertIn("Лучшая точка: Kafe B", self.last())

    def test_single_venue_gets_a_hint(self):
        hub = HubTestCase()
        hub.setUp()
        venue = hub.connect(222)
        hub.hub.set_snapshot(venue, snapshot())
        hub.say(222, "/tarmoq")
        self.assertIn("ikki va undan", hub.api.texts_to(222)[-1])

    def test_venue_without_data_is_listed(self):
        c, reply = self.register("Kafe C")
        self.say(111, f"/start {reply['code']}")
        self.say(111, "/tarmoq")
        self.assertIn("• Kafe C: Hali maʼlumot kelmagan", self.last())
        self.assertIn("4 500 000 soʻm", self.last())

    def test_quiet_venue_is_listed_separately(self):
        self.hub.set_snapshot(self.a, snapshot(venue="Kafe A", today=day(0, 0)))
        self.say(111, "/tarmoq")
        self.assertIn("• Kafe A: Hali savdo yoʻq", self.last())
        self.assertIn("1. Kafe B", self.last())

    def test_button_for_network_and_callback(self):
        self.say(111, "/bugun")
        keyboard = self.api.sent[-1][2]
        flat = [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
        self.assertIn("cmd:network", flat)
        self.api.sent.clear()
        self.hub.process_update({"callback_query": {"id": "1", "data": "cmd:network",
                                                    "message": {"chat": {"id": 111}, "message_id": 5}}})
        self.assertIn("4 500 000", self.last())

    def test_no_button_for_a_single_venue(self):
        hub = HubTestCase()
        hub.setUp()
        venue = hub.connect(333)
        hub.hub.set_snapshot(venue, snapshot())
        hub.say(333, "/bugun")
        flat = [b["callback_data"] for row in hub.api.sent[-1][2]["inline_keyboard"] for b in row]
        self.assertNotIn("cmd:network", flat)

    def test_command_is_in_the_bot_menu(self):
        self.hub.setup_commands()
        sent = [p["commands"] for m, p in self.api.calls if m == "setMyCommands"]
        self.assertTrue(sent)
        self.assertTrue(all(any(c["command"] == "tarmoq" for c in commands) for commands in sent))


class NetworkMorningTests(HubTestCase):
    """Общий утренний отчёт: чату с несколькими точками приходит одно сообщение вместо отдельных итогов."""

    def setUp(self):
        super().setUp()
        self.a, reply_a = self.register("Kafe A")
        self.say(111, f"/start {reply_a['code']}")
        self.b, reply_b = self.register("Kafe B")
        self.say(111, f"/start {reply_b['code']}")
        self.hub.set_snapshot(self.a, snapshot(venue="Kafe A"))
        self.hub.set_snapshot(self.b, snapshot(venue="Kafe B", yesterday=day(1000000, 10, date="2026-11-07")))
        self.api.sent.clear()

    def morning_texts(self):
        return [t for t in self.api.texts_to(111) if "Xayrli tong" in t]

    def test_one_combined_message_instead_of_two(self):
        self.hub.morning_tick()
        morning = self.morning_texts()
        self.assertEqual(len(morning), 1)
        self.assertIn("🌐 Tarmoq · Kecha", morning[0])
        self.assertIn("3 000 000 soʻm", morning[0])  # 2 000 000 + 1 000 000
        self.assertLess(morning[0].index("1. Kafe A"), morning[0].index("2. Kafe B"))

    def test_shopping_stays_per_venue(self):
        self.hub.morning_tick()
        shopping = [t for t in self.api.texts_to(111) if "Mol goʻshti" in t]
        self.assertEqual(len(shopping), 2)

    def test_sent_once_per_day(self):
        self.hub.morning_tick()
        self.hub.morning_tick()
        self.assertEqual(len(self.morning_texts()), 1)

    def test_waits_for_a_late_till_then_sends_what_it_has(self):
        self.hub.set_snapshot(self.b, snapshot(venue="Kafe B", today=day(500000, 5, date="2026-11-08"),
                                               yesterday=None))
        self.api.sent.clear()
        self.hub.morning_tick()  # 09:30: касса B ещё не прислала вчерашний день
        self.assertEqual(self.morning_texts(), [])
        self.now = datetime(2026, 11, 8, 5, 30, tzinfo=timezone.utc).timestamp()  # 10:30 — ещё ждём
        self.hub.morning_tick()
        self.assertEqual(self.morning_texts(), [])
        self.now = datetime(2026, 11, 8, 6, 30, tzinfo=timezone.utc).timestamp()  # 11:30 — последний час окна
        self.hub.morning_tick()
        morning = self.morning_texts()
        self.assertEqual(len(morning), 1)
        self.assertIn("2 000 000 soʻm", morning[0])
        self.assertIn("• Kafe B", morning[0])

    def test_nothing_after_the_window(self):
        self.now = datetime(2026, 11, 8, 7, 30, tzinfo=timezone.utc).timestamp()  # 12:30 по Ташкенту
        self.hub.morning_tick()
        self.assertEqual(self.morning_texts(), [])

    def test_respects_chat_settings(self):
        self.store.set_off(111, {"morning"})
        self.hub.morning_tick()
        self.assertEqual(self.morning_texts(), [])

    def test_no_orders_anywhere_sends_nothing(self):
        self.hub.set_snapshot(self.a, snapshot(venue="Kafe A", yesterday=day(0, 0, date="2026-11-07")))
        self.hub.set_snapshot(self.b, snapshot(venue="Kafe B", yesterday=day(0, 0, date="2026-11-07")))
        self.api.sent.clear()
        self.hub.morning_tick()
        self.assertEqual(self.morning_texts(), [])

    def test_single_venue_chat_keeps_its_own_morning(self):
        other = HubTestCase()
        other.setUp()
        venue = other.connect(444)
        other.hub.set_snapshot(venue, snapshot())
        other.api.sent.clear()
        other.hub.morning_tick()
        first = other.api.texts_to(444)[0]
        self.assertIn("Xayrli tong", first)
        self.assertNotIn("Tarmoq", first)

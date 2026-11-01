import logging
import os
import sqlite3
import tempfile
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from core import error_alerts, telegram
from core.models import CafeSettings

BACKUP = "reports.management.commands.backup_db"


class BackupToTelegramTests(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        db = os.path.join(self.tmp, "db.sqlite3")
        sqlite3.connect(db).close()
        fake = SimpleNamespace(BASE_DIR=self.tmp, DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": db}})
        patcher = mock.patch(f"{BACKUP}.settings", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _connect_bot(self):
        cafe = CafeSettings.get()
        cafe.telegram_bot_token, cafe.telegram_chat_id = "123:abc", "42"
        cafe.save()

    def test_copy_is_sent_when_bot_is_connected(self):
        self._connect_bot()
        with mock.patch("core.telegram.send_document", return_value=(True, "")) as send:
            out = StringIO()
            call_command("backup_db", "--telegram", stdout=out)
        path = send.call_args.args[0]
        self.assertTrue(os.path.basename(path).startswith("db-"))
        self.assertIn("Telegram", out.getvalue())

    def test_without_bot_copy_stays_on_disk(self):
        with mock.patch("core.telegram.send_document") as send:
            out = StringIO()
            call_command("backup_db", "--telegram", stdout=out)
        send.assert_not_called()
        self.assertIn("не подключён", out.getvalue())
        self.assertEqual(len(os.listdir(os.path.join(self.tmp, "backups"))), 1)

    def test_failed_send_is_reported(self):
        self._connect_bot()
        with mock.patch("core.telegram.send_document", return_value=(False, "нет связи")):
            with self.assertRaises(CommandError):
                call_command("backup_db", "--telegram", stdout=StringIO())

    def test_send_document_posts_file_to_bot(self):
        self._connect_bot()
        path = os.path.join(self.tmp, "copy.sqlite3")
        with open(path, "wb") as fh:
            fh.write(b"data")
        response = mock.MagicMock(status=200)
        response.__enter__.return_value = response
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            ok, _reason = telegram.send_document(path, "копия")
        self.assertTrue(ok)
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/sendDocument"))
        self.assertIn(b'filename="copy.sqlite3"', request.data)
        self.assertIn(b"data", request.data)


@override_settings(TELEGRAM_SYNC=True)
class ServerErrorAlertTests(TestCase):
    def setUp(self):
        error_alerts.TelegramErrorHandler._last_sent = 0.0
        cafe = CafeSettings.get()
        cafe.telegram_bot_token, cafe.telegram_chat_id = "123:abc", "42"
        cafe.save()

    def _log_error(self):
        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("django.request").error(
                "Internal Server Error", exc_info=True, extra={"request": SimpleNamespace(path="/orders/7/pay/")}
            )

    def test_server_error_reaches_owner_once_per_pause(self):
        with mock.patch("core.telegram._post", return_value=(True, "")) as post:
            self._log_error()
            self._log_error()
        self.assertEqual(post.call_count, 1)
        text = post.call_args.args[2]
        self.assertIn("/orders/7/pay/", text)
        self.assertIn("ValueError", text)
        self.assertNotIn("boom", text)

    def test_nothing_is_sent_without_bot(self):
        cafe = CafeSettings.get()
        cafe.telegram_bot_token = ""
        cafe.save()
        with mock.patch("core.telegram._post") as post:
            self._log_error()
        post.assert_not_called()

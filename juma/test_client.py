import io
import json
import urllib.error
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from . import client, worker
from .models import JumaState

JUMA = {"JUMA_URL": "https://juma.test", "JUMA_MERCHANT_KEY": "jk_00000000_secret"}


def fake_call(responses=None, default=(200, {})):
    """Подменяет сетевой вызов: responses — {(метод, путь без параметров): (код, тело)}. Запоминает вызовы."""
    calls = []

    def call(method, path, payload=None, timeout=client.TIMEOUT):
        calls.append((method, path, payload))
        return (responses or {}).get((method, path.split("?")[0]), default)

    call.calls = calls
    return call


def fake_response(status, body):
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.status = status
    response.read.return_value = json.dumps(body).encode()
    return response


class ClientTests(TestCase):
    def test_enabled_needs_both_url_and_key(self):
        self.assertFalse(client.enabled())
        with override_settings(JUMA_URL="https://juma.test", JUMA_MERCHANT_KEY=""):
            self.assertFalse(client.enabled())
        with override_settings(**JUMA):
            self.assertTrue(client.enabled())

    @override_settings(**JUMA)
    def test_request_carries_key_json_body_and_full_url(self):
        with mock.patch("urllib.request.urlopen", return_value=fake_response(200, {"delivery_enabled": True})) as opened:
            code, body = client.heartbeat(accepting_orders=False, shift_open=True)
        request = opened.call_args.args[0]
        self.assertEqual((code, body), (200, {"delivery_enabled": True}))
        self.assertEqual(request.full_url, "https://juma.test/api/merchant/v1/heartbeat")
        self.assertEqual(request.get_header("Authorization"), "Bearer jk_00000000_secret")
        self.assertEqual(json.loads(request.data), {"pos": "CAFEPOS", "pos_version": "1.0", "accepting_orders": False, "shift_open": True})

    @override_settings(**JUMA)
    def test_http_errors_keep_code_and_body_and_network_failure_is_code_zero(self):
        error = urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO(b'{"error": {"code": "UNAUTHENTICATED"}}'))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertEqual(client._call("GET", "/catalog"), (401, {"error": {"code": "UNAUTHENTICATED"}}))
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            self.assertEqual(client._call("GET", "/catalog"), (0, {}))
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError()):
            self.assertEqual(client._call("GET", "/catalog"), (0, {}))

    def test_reasons_are_readable(self):
        self.assertIn("Нет связи", client.reason(0))
        self.assertIn("JUMA_MERCHANT_KEY", client.reason(401))
        self.assertIn("приостановлено", client.reason(403))
        self.assertIn("500", client.reason(500, {"error": {"message": "boom"}}))
        self.assertIn("boom", client.reason(500, {"error": {"message": "boom"}}))

    @override_settings(**JUMA)
    def test_long_poll_calls_wait_longer_than_the_server(self):
        with mock.patch("urllib.request.urlopen", return_value=fake_response(200, {"orders": []})) as opened:
            client.pending_orders(wait=25)
            client.events(after=17, wait=25)
        self.assertEqual(opened.call_args_list[0].kwargs["timeout"], client.TIMEOUT + 25)
        self.assertIn("after=17&wait=25", opened.call_args_list[1].args[0].full_url)


@override_settings(**JUMA)
class HeartbeatTests(TestCase):
    def test_success_marks_connection_and_delivery_flag(self):
        call = fake_call({("POST", "/heartbeat"): (200, {"delivery_enabled": True})})
        with mock.patch("juma.client._call", call):
            self.assertTrue(worker.heartbeat_once())
        state = JumaState.get()
        self.assertIsNotNone(state.last_heartbeat_at)
        self.assertTrue(state.delivery_enabled)
        self.assertEqual(state.last_error, "")
        payload = call.calls[0][2]
        self.assertEqual((payload["pos"], payload["accepting_orders"], payload["shift_open"]), ("CAFEPOS", True, False))

    def test_pause_is_reported_to_juma(self):
        JumaState.objects.update_or_create(pk=1, defaults={"accepting_orders": False})
        call = fake_call()
        with mock.patch("juma.client._call", call):
            worker.heartbeat_once()
        self.assertFalse(call.calls[0][2]["accepting_orders"])

    def test_failures_are_recorded_and_do_not_raise(self):
        for code, text in ((401, "JUMA_MERCHANT_KEY"), (403, "приостановлено"), (0, "Нет связи")):
            with mock.patch("juma.client._call", fake_call(default=(code, {}))):
                self.assertFalse(worker.heartbeat_once())
            self.assertIn(text, JumaState.get().last_error)
        self.assertIsNone(JumaState.get().last_heartbeat_at)

    def test_nothing_is_sent_when_juma_is_not_configured(self):
        call = fake_call()
        with override_settings(JUMA_URL="", JUMA_MERCHANT_KEY=""), mock.patch("juma.client._call", call):
            self.assertFalse(worker.heartbeat_once())
        self.assertEqual(call.calls, [])

    def test_guarded_step_swallows_errors_and_reports_them(self):
        JumaState.get()

        def broken():
            raise RuntimeError("boom")

        self.assertFalse(worker._guarded(broken, "связь"))
        self.assertIn("связь", JumaState.get().last_error)


class CommandTests(TestCase):
    def test_refuses_to_run_without_settings(self):
        with self.assertRaises(CommandError):
            call_command("juma_sync", "--once")

    @override_settings(**JUMA)
    def test_once_reports_connection(self):
        out = io.StringIO()
        with mock.patch("juma.client._call", fake_call({("POST", "/heartbeat"): (200, {})})):
            call_command("juma_sync", "--once", stdout=out)
        self.assertIn("есть", out.getvalue())

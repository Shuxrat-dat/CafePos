import base64
import importlib.util
import io
import json
import socket
import threading
import zipfile
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from menu.models import Category, Dish
from pos import services as pos_services
from pos.models import Hall, Order, OrderItem, Table

from . import services
from .escpos import CUT, INIT, Ticket
from .models import PrintAgent, Printer, PrintJob

User = get_user_model()


def text_of(job):
    return bytes(job.data).decode("cp866", errors="replace")


class TicketTests(TestCase):
    def test_init_codepage_cyrillic_and_cut(self):
        t = Ticket(width=48, encoding="cp866", codepage=17)
        t.text("Плов")
        data = t.finish(cut=True)
        self.assertTrue(data.startswith(INIT + b"\x1bt\x11"))
        self.assertIn("Плов".encode("cp866"), data)
        self.assertTrue(data.endswith(CUT))

    def test_no_cut_when_disabled(self):
        self.assertFalse(Ticket().finish(cut=False).endswith(CUT))

    def test_pair_fills_the_line(self):
        t = Ticket(width=32)
        start = len(t.buf)
        t.pair("Чай", "5 000")
        line = bytes(t.buf[start:]).split(b"\n")[0].replace(b"\x1bE\x00", b"")
        self.assertEqual(len(line.decode("cp866")), 32)

    def test_uzbek_apostrophe_is_printable(self):
        t = Ticket(encoding="cp866")
        t.text("Oʻzbekcha soʻm")
        self.assertIn(b"O'zbekcha so'm", t.finish())


class PrintFlowTests(TestCase):
    def setUp(self):
        self.cashier = User.objects.create_user(username="kassa", password="pass12345", role="cashier")
        table = Table.objects.create(hall=Hall.objects.create(name="Зал"), number="5")
        self.hot = Category.objects.create(name="Горячее")
        self.drinks = Category.objects.create(name="Напитки")
        self.plov = Dish.objects.create(category=self.hot, name="Плов", price=45000)
        self.tea = Dish.objects.create(category=self.drinks, name="Чай", price=5000)
        self.order = Order.objects.create(table=table, waiter=self.cashier)
        self.receipt_printer = Printer.objects.create(name="Касса", host="192.168.1.50")
        self.kitchen = Printer.objects.create(name="Кухня", host="192.168.1.51", prints_receipts=False, prints_kitchen=True)
        self.kitchen.categories.set([self.hot])
        self.bar = Printer.objects.create(name="Бар", host="192.168.1.52", prints_receipts=False, prints_kitchen=True)
        self.bar.categories.set([self.drinks])
        self.client.force_login(self.cashier)

    def test_kitchen_items_go_to_printers_by_category(self):
        OrderItem.objects.create(order=self.order, dish=self.plov, quantity=2, comment="без лука")
        OrderItem.objects.create(order=self.order, dish=self.tea, quantity=1)
        with self.captureOnCommitCallbacks(execute=True):
            pos_services.send_to_kitchen(self.order)
        kitchen = PrintJob.objects.get(printer=self.kitchen)
        bar = PrintJob.objects.get(printer=self.bar)
        self.assertIn("Плов", text_of(kitchen))
        self.assertIn("без лука", text_of(kitchen))
        self.assertNotIn("Чай", text_of(kitchen))
        self.assertIn("Чай", text_of(bar))
        self.assertFalse(PrintJob.objects.filter(printer=self.receipt_printer).exists())

    def test_second_send_is_marked_as_addition(self):
        OrderItem.objects.create(order=self.order, dish=self.plov, quantity=1)
        with self.captureOnCommitCallbacks(execute=True):
            pos_services.send_to_kitchen(self.order)
        OrderItem.objects.create(order=self.order, dish=self.plov, quantity=1)
        with self.captureOnCommitCallbacks(execute=True):
            pos_services.send_to_kitchen(self.order)
        first, second = PrintJob.objects.filter(printer=self.kitchen).order_by("id")
        self.assertNotIn("ДОЗАКАЗ", text_of(first))
        self.assertIn("ДОЗАКАЗ", text_of(second))

    def test_kitchen_printer_without_categories_gets_everything(self):
        self.bar.categories.clear()
        OrderItem.objects.create(order=self.order, dish=self.plov, quantity=1)
        with self.captureOnCommitCallbacks(execute=True):
            pos_services.send_to_kitchen(self.order)
        self.assertIn("Плов", text_of(PrintJob.objects.get(printer=self.bar)))

    def test_receipt_is_printed_after_payment(self):
        OrderItem.objects.create(order=self.order, dish=self.plov, quantity=2, status=OrderItem.Status.SERVED)
        manual = Printer.objects.create(name="Второй", host="192.168.1.53", auto_receipt=False)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("pay", args=[self.order.id]), {"method": "cash", "received": "100000"})
        self.assertRedirects(response, reverse("receipt", args=[self.order.id]))
        job = PrintJob.objects.get(printer=self.receipt_printer, kind=PrintJob.Kind.RECEIPT)
        self.assertEqual(job.order_id, self.order.id)
        body = text_of(job)
        self.assertIn("90 000", body)
        self.assertIn("10 000", body)  # сдача
        self.assertFalse(PrintJob.objects.filter(printer=manual).exists())

    def test_printing_failure_does_not_break_payment(self):
        OrderItem.objects.create(order=self.order, dish=self.plov, quantity=1, status=OrderItem.Status.SERVED)
        with mock.patch("printing.documents.receipt", side_effect=RuntimeError("printer bug")):
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse("pay", args=[self.order.id]), {"method": "card"})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)

    def test_copies(self):
        self.receipt_printer.copies = 2
        self.receipt_printer.save()
        OrderItem.objects.create(order=self.order, dish=self.tea, quantity=1)
        self.assertEqual(len(services.queue_receipt(self.order, precheck=True)), 2)

    def test_print_button_on_receipt_and_permissions(self):
        OrderItem.objects.create(order=self.order, dish=self.tea, quantity=1, status=OrderItem.Status.SERVED)
        self.client.post(reverse("pay", args=[self.order.id]), {"method": "card"})
        page = self.client.get(reverse("receipt", args=[self.order.id]))
        self.assertContains(page, "На принтер")
        self.assertContains(page, "window.print()")

        response = self.client.post(reverse("print_order", args=[self.order.id]), {"kind": "receipt"})
        self.assertRedirects(response, reverse("receipt", args=[self.order.id]), fetch_redirect_response=False)
        self.assertTrue(PrintJob.objects.filter(printer=self.receipt_printer, kind=PrintJob.Kind.RECEIPT).exists())

        waiter = User.objects.create_user(username="ofitsiant", password="pass12345", role="waiter")
        self.client.force_login(waiter)
        response = self.client.post(reverse("print_order", args=[self.order.id]), {"kind": "receipt"})
        self.assertEqual(response.status_code, 403)

    def test_print_status_shows_error(self):
        job = services.queue_test(self.receipt_printer)
        job.order = self.order
        job.status = PrintJob.Status.SENT
        job.save()
        services.finish_job(job, False, "timeout", "192.168.1.50:9100")
        response = self.client.get(reverse("order_print_status", args=[self.order.id]))
        self.assertContains(response, "192.168.1.50:9100")
        self.assertContains(response, "Ошибка")


class AgentApiTests(TestCase):
    def setUp(self):
        self.printer = Printer.objects.create(name="Касса", host="192.168.1.50", port=9100)
        self.agent = PrintAgent.get()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.agent.token}"}

    def test_wrong_token_is_rejected(self):
        response = self.client.get(reverse("print_agent_jobs"), HTTP_AUTHORIZATION="Bearer nope")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.get(reverse("print_agent_jobs")).status_code, 401)

    def test_claim_and_report_success(self):
        job = services.queue_test(self.printer)
        data = self.client.get(reverse("print_agent_jobs"), HTTP_X_AGENT_INFO="KASSA-PC v1.0", **self.auth).json()
        self.assertEqual(len(data["jobs"]), 1)
        sent = data["jobs"][0]
        self.assertEqual(sent["printer"]["host"], "192.168.1.50")
        self.assertEqual(base64.b64decode(sent["data"]), bytes(job.data))
        job.refresh_from_db()
        self.assertEqual(job.status, PrintJob.Status.SENT)
        # Повторный опрос не отдаёт то же задание второй раз
        self.assertEqual(self.client.get(reverse("print_agent_jobs"), **self.auth).json()["jobs"], [])

        self.client.post(reverse("print_agent_result", args=[job.id]), json.dumps({"ok": True}),
                         content_type="application/json", **self.auth)
        job.refresh_from_db()
        self.assertEqual(job.status, PrintJob.Status.DONE)
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.is_online)
        self.assertEqual(self.agent.info, "KASSA-PC v1.0")

    def test_report_error_is_translated(self):
        job = services.queue_test(self.printer)
        self.client.get(reverse("print_agent_jobs"), **self.auth)
        self.client.post(reverse("print_agent_result", args=[job.id]),
                         json.dumps({"ok": False, "code": "refused", "detail": "192.168.1.50:9100"}),
                         content_type="application/json", **self.auth)
        job.refresh_from_db()
        self.assertEqual(job.status, PrintJob.Status.ERROR)
        self.assertIn("192.168.1.50:9100", job.error)

    def test_inactive_printer_jobs_wait(self):
        services.queue_test(self.printer)
        self.printer.is_active = False
        self.printer.save()
        self.assertEqual(self.client.get(reverse("print_agent_jobs"), **self.auth).json()["jobs"], [])


class ConnectionCheckTests(TestCase):
    """Проверка связи с принтерами: три чековых принтера по IP, как в заведении."""

    def setUp(self):
        self.printers = [Printer.objects.create(name=f"Касса {n}", host=f"192.168.1.{100 + n}") for n in (1, 2, 3)]
        self.agent = PrintAgent.get()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.agent.token}"}
        self.owner = User.objects.create_user(username="direktor", password="pass12345", role="owner")

    def poll(self):
        return self.client.get(reverse("print_agent_jobs"), **self.auth).json()

    def report(self, results):
        self.client.post(reverse("print_agent_status"), json.dumps({"printers": results}),
                         content_type="application/json", **self.auth)

    def test_agent_gets_printers_to_check_every_half_minute(self):
        first = self.poll()["check"]
        self.assertEqual({p["host"] for p in first}, {"192.168.1.101", "192.168.1.102", "192.168.1.103"})
        self.assertEqual(self.poll()["check"], [])  # сразу же повторно не просим
        PrintAgent.objects.update(check_sent_at=timezone.now() - timedelta(seconds=40))
        self.assertEqual(len(self.poll()["check"]), 3)

    def test_check_button_asks_for_check_now(self):
        self.poll()
        self.assertEqual(self.poll()["check"], [])
        self.client.force_login(self.owner)
        self.client.post(reverse("manage_printers"), {"action": "check"})
        self.assertEqual(len(self.poll()["check"]), 3)

    def test_results_are_shown_to_director(self):
        self.poll()
        a, b, c = self.printers
        self.report([{"id": a.id, "ok": True}, {"id": b.id, "ok": True},
                     {"id": c.id, "ok": False, "code": "timeout", "detail": "192.168.1.103:9100"}])
        c.refresh_from_db()
        self.assertEqual(c.connection_state, "error")
        self.assertIn("192.168.1.103:9100", c.check_error)
        self.client.force_login(self.owner)
        page = self.client.get(reverse("manage_printers"))
        self.assertContains(page, "Связь есть", count=2)
        self.assertContains(page, "Нет связи")
        self.assertEqual(services.problem_count(), 1)
        # Отдельный пункт «Принтеры» в меню директора с числом проблемных принтеров
        nav = {item["name"]: item for item in page.context["nav_items"]}
        self.assertEqual(nav["manage_printers"]["badge"], 1)

    def test_old_result_is_not_trusted(self):
        self.poll()
        self.report([{"id": self.printers[0].id, "ok": True}])
        Printer.objects.filter(id=self.printers[0].id).update(checked_at=timezone.now() - timedelta(minutes=5))
        self.printers[0].refresh_from_db()
        self.assertEqual(self.printers[0].connection_state, "unknown")

    def test_menu_counts_all_printers_when_agent_is_offline(self):
        self.assertEqual(services.problem_count(), 3)

    def test_printers_menu_is_only_for_managers(self):
        cashier = User.objects.create_user(username="k2", password="pass12345", role="cashier")
        self.client.force_login(cashier)
        nav = {item["name"] for item in self.client.get(reverse("floor")).context["nav_items"]}
        self.assertNotIn("manage_printers", nav)


class ManagePrintersTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="egasi", password="pass12345", role="owner")
        self.client.force_login(self.owner)

    def test_create_network_printer_and_test_print(self):
        response = self.client.post(reverse("printer_create"), {
            "name": "Касса", "connection": "network", "host": "192.168.1.200", "port": "9100",
            "prints_receipts": "on", "auto_receipt": "on", "copies": "1", "line_width": "48",
            "encoding": "cp866", "codepage": "17", "cut_paper": "on", "is_active": "on", "action": "test",
        })
        self.assertRedirects(response, reverse("manage_printers"))
        printer = Printer.objects.get()
        self.assertEqual(printer.address, "192.168.1.200:9100")
        self.assertEqual(PrintJob.objects.get().kind, PrintJob.Kind.TEST)

    def test_network_printer_needs_ip(self):
        response = self.client.post(reverse("printer_create"), {
            "name": "Касса", "connection": "network", "host": "", "port": "9100", "prints_receipts": "on",
            "copies": "1", "line_width": "48", "encoding": "cp866", "codepage": "17",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Printer.objects.exists())

    def test_page_and_test_button(self):
        printer = Printer.objects.create(name="Кухня", host="10.0.0.5", prints_kitchen=True)
        page = self.client.get(reverse("manage_printers"))
        self.assertContains(page, "Кухня")
        self.assertContains(page, "Не на связи")
        self.client.post(reverse("printer_test", args=[printer.id]))
        self.assertEqual(PrintJob.objects.filter(printer=printer, kind=PrintJob.Kind.TEST).count(), 1)

    def test_agent_download_has_token_and_windows_line_endings(self):
        response = self.client.get(reverse("print_agent_download"))
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        names = set(archive.namelist())
        self.assertIn("cafepos_print_agent/cafepos_print_agent.py", names)
        ini = archive.read("cafepos_print_agent/cafepos_print_agent.ini").decode()
        self.assertIn(PrintAgent.get().token, ini)
        self.assertIn("\r\n", archive.read("cafepos_print_agent/install_print_agent.bat").decode())

    def test_cashier_cannot_open_printer_settings(self):
        self.client.force_login(User.objects.create_user(username="k", password="pass12345", role="cashier"))
        self.assertEqual(self.client.get(reverse("manage_printers")).status_code, 403)


class AgentProgramTests(TestCase):
    """Сама программа печати: отправка байтов на сетевой принтер по TCP."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = settings.BASE_DIR / "print_agent" / "cafepos_print_agent.py"
        spec = importlib.util.spec_from_file_location("cafepos_print_agent", path)
        cls.agent = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.agent)

    def test_sends_bytes_to_network_printer(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        received = bytearray()

        def accept():
            conn, _addr = server.accept()
            with conn:
                while chunk := conn.recv(4096):
                    received.extend(chunk)

        thread = threading.Thread(target=accept)
        thread.start()
        payload = Ticket().text("Тест").finish()
        self.agent.send({"connection": "network", "host": "127.0.0.1", "port": server.getsockname()[1]}, payload)
        thread.join(5)
        server.close()
        self.assertEqual(bytes(received), payload)

    def _listener(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(5)

        def accept_until_closed():
            while True:
                try:
                    server.accept()[0].close()
                except OSError:
                    return

        threading.Thread(target=accept_until_closed, daemon=True).start()
        return server

    def _closed_port(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def test_check_three_printers_without_printing(self):
        up1, up2 = self._listener(), self._listener()
        printers = [
            {"id": 1, "name": "Kassa 1", "connection": "network", "host": "127.0.0.1", "port": up1.getsockname()[1]},
            {"id": 2, "name": "Kassa 2", "connection": "network", "host": "127.0.0.1", "port": up2.getsockname()[1]},
            {"id": 3, "name": "Kassa 3", "connection": "network", "host": "127.0.0.1", "port": self._closed_port()},
        ]
        sent = []

        class FakeServer:
            def send_check(self, results):
                sent.extend(results)

        with mock.patch.object(self.agent, "CONNECT_RETRY_DELAY", 0):
            self.agent.check_all(FakeServer(), printers)
        up1.close()
        up2.close()
        self.assertEqual([r["ok"] for r in sent], [True, True, False])
        self.assertIn(sent[2]["code"], ("refused", "network"))

    def test_connect_is_retried_before_error(self):
        calls = []
        real = socket.create_connection

        def flaky(address, timeout=None):
            calls.append(address)
            if len(calls) < 3:
                raise ConnectionRefusedError
            return real(address, timeout=timeout)

        server = self._listener()
        with mock.patch.object(self.agent.socket, "create_connection", side_effect=flaky), \
                mock.patch.object(self.agent, "CONNECT_RETRY_DELAY", 0):
            self.agent.send_network("127.0.0.1", server.getsockname()[1], b"x")
        server.close()
        self.assertEqual(len(calls), 3)

    def test_closed_port_reports_refused(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(self.agent.PrintError) as ctx:
            self.agent.send_network("127.0.0.1", port, b"x")
        self.assertIn(ctx.exception.code, ("refused", "network"))

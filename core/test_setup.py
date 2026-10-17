import io
import sqlite3
import tempfile
import zipfile
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from menu import importer
from menu.models import Category, Dish
from pos.models import Hall, Table

from .menu_templates import TEMPLATES, rows_for
from .models import AuditLog, CafeSettings

User = get_user_model()


def make_xlsx(rows):
    """Минимальный .xlsx, какой сохраняет Excel: общие строки и числа."""
    shared, cells = [], []
    for r, row in enumerate(rows, start=1):
        line = []
        for c, value in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if isinstance(value, (int, float)):
                line.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                shared.append(value)
                line.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
        cells.append(f'<row r="{r}">{"".join(line)}</row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{ns}">' + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{ns}"><sheetData>{"".join(cells)}</sheetData></worksheet>')
    return buffer.getvalue()


class ImporterTests(TestCase):
    def read(self, data, name="menu.csv"):
        return importer.read_rows(SimpleUploadedFile(name, data))

    def test_csv_with_semicolons_and_header(self):
        data = "Категория;Название;Цена\nГорячее;Плов;45 000\nНапитки;Чай;8000 сум\n".encode("utf-8-sig")
        report = importer.import_rows(self.read(data))
        self.assertEqual((report["created"], report["skipped"]), (2, 0))
        self.assertEqual(Dish.objects.get(name="Плов").price, Decimal(45000))
        self.assertEqual(Dish.objects.get(name="Чай").price, Decimal(8000))

    def test_windows_1251_and_commas_are_understood(self):
        data = "Категория,Название,Цена\nСалаты,Цезарь,38000\n".encode("cp1251")
        importer.import_rows(self.read(data))
        self.assertTrue(Dish.objects.filter(name="Цезарь", category__name="Салаты").exists())

    def test_xlsx_file(self):
        data = make_xlsx([["Категория", "Название", "Цена", "Время приготовления"], ["Кофе", "Латте", 24000, 4]])
        importer.import_rows(self.read(data, "menu.xlsx"))
        dish = Dish.objects.get(name="Латте")
        self.assertEqual((dish.price, dish.prep_minutes), (Decimal(24000), 4))

    def test_file_without_header_uses_default_order(self):
        importer.import_rows(self.read("Десерты;Чизкейк;28000\n".encode("utf-8")))
        self.assertTrue(Dish.objects.filter(name="Чизкейк").exists())

    def test_reimport_updates_price_instead_of_duplicating(self):
        importer.import_rows(self.read("Категория;Название;Цена\nГорячее;Плов;45000\n".encode()))
        report = importer.import_rows(self.read("Категория;Название;Цена\nГорячее;плов;50000\n".encode()))
        self.assertEqual((report["created"], report["updated"]), (0, 1))
        self.assertEqual(Dish.objects.count(), 1)
        self.assertEqual(Dish.objects.get().price, Decimal(50000))

    def test_bad_rows_are_reported_and_good_ones_imported(self):
        data = "Категория;Название;Цена\nГорячее;Плов;45000\nГорячее;Манты;abc\nГорячее;;1000\nГорячее;Шурпа;-5\n".encode()
        report = importer.import_rows(self.read(data))
        self.assertEqual((report["created"], report["skipped"]), (1, 3))
        self.assertEqual(len(report["errors"]), 3)

    def test_broken_and_oversized_files_give_clear_errors(self):
        with self.assertRaises(importer.ImportProblem):
            self.read(b"PK\x03\x04 not really a zip", "x.xlsx")
        with self.assertRaises(importer.ImportProblem):
            self.read(b"", "empty.csv")
        with self.assertRaises(importer.ImportProblem):
            self.read(b"a;b;c\n" * 200_000, "big.csv")

    def test_every_template_imports_cleanly(self):
        for key in TEMPLATES:
            Dish.objects.all().delete()
            Category.objects.all().delete()
            report = importer.import_rows(rows_for(key))
            self.assertEqual(report["errors"], [], key)
            self.assertEqual(report["created"], len(TEMPLATES[key][2]))


class SetupWizardTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="own_s", password="x", role="owner")
        self.waiter = User.objects.create_user(username="wt_s", password="x", role="waiter")

    def test_waiter_has_no_access(self):
        self.client.force_login(self.waiter)
        self.assertEqual(self.client.get(reverse("setup_wizard")).status_code, 403)

    def test_banner_shows_on_empty_install_and_disappears_when_set_up(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("floor")), "Открыть мастер")
        self.client.post(reverse("setup_wizard"), {"action": "tables", "hall_name": "Зал", "count": "3", "seats": "4"})
        self.client.post(reverse("setup_wizard"), {"action": "template", "template": "coffee"})
        self.assertNotContains(self.client.get(reverse("floor")), "Открыть мастер")

    def test_banner_hidden_after_finish_even_if_empty(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("setup_wizard"), {"action": "finish"})
        self.assertTrue(CafeSettings.get().setup_done)
        self.assertNotContains(self.client.get(reverse("floor")), "Открыть мастер")

    def test_cafe_name_is_saved_and_required(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("setup_wizard"), {"action": "cafe", "name": "Чайхана", "address": "ул. 1",
                                                   "phone": "", "default_language": "ru"})
        self.assertEqual(CafeSettings.get().name, "Чайхана")
        self.client.post(reverse("setup_wizard"), {"action": "cafe", "name": "  ", "default_language": "ru"})
        self.assertEqual(CafeSettings.get().name, "Чайхана")

    def test_tables_are_created_once_and_numbers_do_not_clash(self):
        self.client.force_login(self.owner)
        data = {"action": "tables", "hall_name": "Терраса", "count": "5", "seats": "2"}
        self.client.post(reverse("setup_wizard"), data)
        self.client.post(reverse("setup_wizard"), {**data, "hall_name": "терраса", "count": "7"})
        hall = Hall.objects.get()
        self.assertEqual(sorted(hall.tables.values_list("number", flat=True)), ["1", "2", "3", "4", "5", "6", "7"])
        self.assertEqual(Table.objects.filter(seats=2).count(), 7)

    def test_bad_table_numbers_are_refused(self):
        self.client.force_login(self.owner)
        for count, seats in (("0", "4"), ("500", "4"), ("x", "4"), ("5", "0"), ("5", "99")):
            self.client.post(reverse("setup_wizard"), {"action": "tables", "count": count, "seats": seats})
        self.assertEqual(Table.objects.count(), 0)

    def test_template_and_upload_flow_with_audit(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("setup_wizard"), {"action": "template", "template": "uzbek"})
        self.assertEqual(Dish.objects.count(), len(TEMPLATES["uzbek"][2]))
        upload = SimpleUploadedFile("my.csv", "Категория;Название;Цена\nДругое;Моё блюдо;10000\nДругое;Плохое;x\n".encode())
        response = self.client.post(reverse("setup_wizard"), {"action": "import", "file": upload}, follow=True)
        self.assertTrue(Dish.objects.filter(name="Моё блюдо").exists())
        self.assertContains(response, "строка 3")
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.MENU_IMPORT).count(), 2)

    def test_unknown_template_and_missing_file_are_rejected(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("setup_wizard"), {"action": "template", "template": "../../etc"})
        response = self.client.post(reverse("setup_wizard"), {"action": "import"}, follow=True)
        self.assertEqual(Dish.objects.count(), 0)
        self.assertContains(response, "Выберите файл")

    def test_sample_file_downloads(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("setup_menu_template"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertContains(response, "Категория")


class BackupTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="own_b", password="x", role="owner")
        self.cashier = User.objects.create_user(username="cash_b", password="x", role="cashier")

    def test_only_managers_can_download(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse("manage_backup")).status_code, 403)
        self.assertEqual(self.client.post(reverse("manage_backup"), {"action": "download"}).status_code, 403)

    def test_download_returns_the_snapshot_and_is_logged(self):
        # Тестовая база живёт в памяти, поэтому подменяем снимок небольшой настоящей базой
        handle, path = tempfile.mkstemp(suffix=".sqlite3")
        connection = sqlite3.connect(path)
        connection.execute("create table t(x)")
        connection.execute("insert into t values (42)")
        connection.commit()
        connection.close()
        with open(path, "rb") as file:
            payload = file.read()
        self.client.force_login(self.owner)
        with mock.patch("core.setup_views._sqlite_snapshot", return_value=payload), \
                mock.patch("core.setup_views._is_sqlite", return_value=True):
            response = self.client.post(reverse("manage_backup"), {"action": "download"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("cafepos-", response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"SQLite format 3"))
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.BACKUP).count(), 1)

    def test_page_opens_for_owner(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("manage_backup")), "Скачать копию")

import io
import zipfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from menu.models import Category, Dish
from pos.models import Order, OrderItem

from . import xlsx
from .models import Expense

User = get_user_model()


class XlsxWriterTests(SimpleTestCase):
    def test_workbook_is_a_valid_zip_with_sheets_and_escaped_text(self):
        data = xlsx.build_workbook([
            ("Заказы", [["№", "Сумма"], [1, Decimal("50000")], ["a < b & c", None]]),
            ("Bad:/Name?", [["x"]]),
        ])
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertIsNone(z.testzip())
            names = z.namelist()
            for part in ("[Content_Types].xml", "xl/workbook.xml", "xl/styles.xml",
                         "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"):
                self.assertIn(part, names)
            sheet1 = z.read("xl/worksheets/sheet1.xml").decode()
            self.assertIn("<v>50000</v>", sheet1)
            self.assertIn("a &lt; b &amp; c", sheet1)
            workbook = z.read("xl/workbook.xml").decode()
            self.assertIn("Заказы", workbook)
            self.assertIn("Bad-Name", workbook)  # «:» и «?» убираются, «/» заменяется на «-»

    def test_column_letters(self):
        self.assertEqual([xlsx._col(i) for i in (1, 26, 27, 52, 53)], ["A", "Z", "AA", "AZ", "BA"])


class ExportViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner_x", password="pass12345", role="owner")
        self.cashier = User.objects.create_user(username="cashier_x", password="pass12345", role="cashier")
        category = Category.objects.create(name="Горячее")
        dish = Dish.objects.create(category=category, name="Плов", price=Decimal("50000"))
        order = Order.objects.create(waiter=self.cashier, order_type=Order.OrderType.TAKEAWAY,
                                     status=Order.Status.PAID, closed_at=timezone.now(), payment_method="cash")
        OrderItem.objects.create(order=order, dish=dish, quantity=2, price=dish.price, status=OrderItem.Status.SERVED)
        Expense.objects.create(date=timezone.localdate(), category="rent", amount=30000)

    def test_owner_downloads_xlsx_with_all_sheets(self):
        self.client.force_login(self.owner)
        day = timezone.localdate().isoformat()
        response = self.client.get(reverse("export_xlsx"), {"from": day, "to": day})
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response["Content-Type"])
        self.assertIn(".xlsx", response["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            workbook = z.read("xl/workbook.xml").decode()
            for name in ("Сводка", "Заказы", "Блюда", "Расходы"):
                self.assertIn(name, workbook)
            self.assertIn("Плов", z.read("xl/worksheets/sheet3.xml").decode())
            self.assertIn("<v>100000</v>", z.read("xl/worksheets/sheet1.xml").decode())
            self.assertIn("<v>30000</v>", z.read("xl/worksheets/sheet4.xml").decode())

    def test_cashier_cannot_export(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse("export_xlsx")).status_code, 403)

    def test_bad_dates_fall_back_to_today(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("export_xlsx"), {"from": "oops"}).status_code, 200)

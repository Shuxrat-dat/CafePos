from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import CafeSettings
from menu.models import Category, Dish
from pos.models import Order

from . import documents
from .escpos import GS, SIZE_BIG, Ticket
from .models import Printer

User = get_user_model()
BIG = GS + b"!" + bytes([SIZE_BIG])


class ReceiptStyleTests(TestCase):
    """Печатный чек: название заведения и итог крупно, между блюдами пунктир."""

    def setUp(self):
        cafe = CafeSettings.get()
        cafe.name = "SKAZKA"
        cafe.save()
        self.printer = Printer.objects.create(name="Касса", host="192.168.1.50")
        waiter = User.objects.create_user(username="ofi_style", password="x", role="waiter")
        category = Category.objects.create(name="Food")
        self.plov = Dish.objects.create(category=category, name="Plov", price=40000)
        self.tea = Dish.objects.create(category=category, name="Tea", price=5000)
        self.order = Order.objects.create(waiter=waiter, order_type="takeaway")

    def _data(self, *dishes):
        # чек оставляет на заказе подставленный список позиций, поэтому каждый раз берём заказ заново
        Order.objects.get(pk=self.order.pk).items.all().delete()
        order = Order.objects.get(pk=self.order.pk)
        for dish in dishes:
            order.items.create(dish=dish, quantity=1, price=dish.price)
        return documents.receipt(Order.objects.get(pk=self.order.pk), self.printer)

    def test_cafe_name_and_total_are_printed_big(self):
        data = self._data(self.plov, self.tea)
        self.assertEqual(data.count(BIG), 2)  # название заведения и итог
        first = data.index(BIG)
        self.assertIn(b"SKAZKA", data[first:first + 40])  # крупный размер включён перед названием

    def test_dashed_line_goes_between_dishes_only(self):
        dashed = (b"- " * 24)[:48]
        self.assertNotIn(dashed, self._data(self.plov))
        self.assertEqual(self._data(self.plov, self.tea).count(dashed), 1)
        self.assertEqual(self._data(self.plov, self.tea, self.plov).count(dashed), 2)

    def test_dashed_line_fits_any_paper_width(self):
        for width in (32, 42, 48):
            t = Ticket(width)
            before = len(t.buf)
            t.dashed()
            self.assertEqual(len(t.buf) - before, width + 1)  # строка во всю ширину и перевод строки

    def test_big_total_uses_half_the_line(self):
        t = Ticket(48)
        before = len(t.buf)
        t.pair("TOTAL", "159 000", bold=True, size=SIZE_BIG)
        line = bytes(t.buf[before:]).split(b"\n")[0]
        self.assertIn(b"TOTAL", line)
        self.assertIn(b"159 000", line)
        self.assertLessEqual(len(line.split(BIG)[-1]), 24)  # при двойной ширине в строке 24 символа, не 48


class ReceiptQuantityRowTests(ReceiptStyleTests):
    """Количество стоит в той же строке, что и название блюда, а не на второй."""

    def _lines(self, data):
        return [part.decode("cp866", errors="replace") for part in data.split(b"\n")]

    def test_quantity_is_in_the_name_row(self):
        order = Order.objects.get(pk=self.order.pk)
        order.items.create(dish=self.plov, quantity=2, price=self.plov.price)
        lines = self._lines(documents.receipt(Order.objects.get(pk=self.order.pk), self.printer))
        row = next(line for line in lines if "Plov" in line)
        self.assertIn("2 x Plov", row)
        self.assertTrue(row.rstrip().endswith("80 000"))  # сумма строки справа, в том же ряду
        self.assertFalse(any(line.strip().startswith("2 x 40 000") for line in lines))  # второй строки нет

    def test_weight_dish_shows_weight_in_the_name_row(self):
        fish = Dish.objects.create(category=self.plov.category, name="Fish", price=100000, sold_by_weight=True)
        order = Order.objects.get(pk=self.order.pk)
        order.items.create(dish=fish, quantity=1, price=fish.price, weight_grams=1500)
        lines = self._lines(documents.receipt(Order.objects.get(pk=self.order.pk), self.printer))
        row = next(line for line in lines if "Fish" in line)
        self.assertRegex(row, r"Fish 1,5 .*150 000")

    def test_on_screen_receipt_has_quantity_before_the_name(self):
        from django.urls import reverse

        order = Order.objects.get(pk=self.order.pk)
        order.items.create(dish=self.plov, quantity=3, price=self.plov.price)
        owner = User.objects.create_user(username="own_style", password="x", role="owner")
        self.client.force_login(owner)
        page = self.client.get(reverse("precheck", args=[order.id]))
        self.assertContains(page, "3 × Plov")

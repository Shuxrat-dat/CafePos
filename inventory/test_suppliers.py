from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Product, StockMovement, Supplier, SupplierPayment

User = get_user_model()


class SupplierTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin_sup", password="pass12345", role="admin")
        self.cashier = User.objects.create_user(username="cashier_sup", password="pass12345", role="cashier")
        self.supplier = Supplier.objects.create(name="Ташкент Мясо")
        self.meat = Product.objects.create(name="Говядина", unit="g", cost_per_unit=Decimal("80"))
        self.client.force_login(self.admin)

    def income(self, **extra):
        data = {"product": self.meat.id, "quantity": "1000", "cost": "90", "supplier": self.supplier.id}
        data.update(extra)
        return self.client.post(reverse("stock"), data)

    def test_income_from_supplier_creates_debt(self):
        self.income()
        movement = StockMovement.objects.get(kind="income")
        self.assertEqual(movement.supplier, self.supplier)
        self.assertEqual(self.supplier.purchased, Decimal("90000"))
        self.assertEqual(self.supplier.debt, Decimal("90000"))

    def test_paid_now_leaves_no_debt(self):
        self.income(paid_now="1")
        self.assertEqual(self.supplier.debt, Decimal("0"))
        self.assertEqual(SupplierPayment.objects.get().amount, Decimal("90000"))

    def test_payment_reduces_debt(self):
        self.income()
        self.client.post(reverse("supplier_detail", args=[self.supplier.id]), {"amount": "40000"})
        self.assertEqual(self.supplier.debt, Decimal("50000"))

    def test_bad_payment_amount_is_rejected(self):
        response = self.client.post(reverse("supplier_detail", args=[self.supplier.id]), {"amount": "abc"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SupplierPayment.objects.exists())

    def test_income_without_supplier_still_works(self):
        self.income(supplier="")
        self.assertIsNone(StockMovement.objects.get(kind="income").supplier)

    def test_add_supplier_and_list(self):
        self.client.post(reverse("suppliers"), {"name": "Овощи Чорсу", "phone": "+998901112233", "is_active": "on"})
        self.assertTrue(Supplier.objects.filter(name="Овощи Чорсу").exists())
        self.assertContains(self.client.get(reverse("suppliers")), "Овощи Чорсу")

    def test_cashier_has_no_access(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse("suppliers")).status_code, 403)
        self.assertEqual(self.client.get(reverse("supplier_detail", args=[self.supplier.id])).status_code, 403)


class IncomeNumpadTests(TestCase):
    def test_income_form_has_number_pad_for_quantity_and_price(self):
        admin = User.objects.create_user(username="admin_pad", password="pass12345", role="admin")
        self.client.force_login(admin)
        html = self.client.get(reverse("stock")).content.decode()
        self.assertIn('data-numpad="in-qty"', html)
        self.assertIn('id="in-qty" name="quantity" value="" inputmode="none"', html)
        self.assertIn('id="in-cost" name="cost" value="" inputmode="none"', html)

    def test_decimal_quantity_with_comma_still_works(self):
        admin = User.objects.create_user(username="admin_pad2", password="pass12345", role="admin")
        product = Product.objects.create(name="Мука", unit="g")
        self.client.force_login(admin)
        self.client.post(reverse("stock"), {"product": product.id, "quantity": "1500,5", "cost": "12,5"})
        product.refresh_from_db()
        self.assertEqual(product.stock, Decimal("1500.5"))
        self.assertEqual(product.cost_per_unit, Decimal("12.50"))

import uuid
from decimal import Decimal

from accounts import cancel_pin
from django.contrib.auth import get_user_model
from django.test import TestCase

from inventory.models import Product, RecipeItem, StockMovement
from menu.models import Category, Dish
from pos import services
from pos.models import CashMovement, CashShift, Hall, Order, OrderItem, Payment, Table

User = get_user_model()


class P0FinancialTests(TestCase):
    def setUp(self):
        self.cashier = User.objects.create_user(username="cashier_p0", password="password123", role="cashier")
        cancel_pin.set_pin(self.cashier, "7391")
        self.waiter = User.objects.create_user(username="waiter_p0", password="password123", role="waiter")
        self.hall = Hall.objects.create(name="Главный зал")
        self.table = Table.objects.create(hall=self.hall, number="10")
        self.category = Category.objects.create(name="Кухня")

        # Продукты и техкарта
        self.meat = Product.objects.create(name="Мясо", stock=Decimal("1000"), unit=Product.Unit.GRAM, cost_per_unit=Decimal("80"))
        self.rice = Product.objects.create(name="Рис", stock=Decimal("2000"), unit=Product.Unit.GRAM, cost_per_unit=Decimal("20"))

        self.dish_plov = Dish.objects.create(category=self.category, name="Плов", price=Decimal("50000"))
        RecipeItem.objects.create(dish=self.dish_plov, product=self.meat, quantity=Decimal("150"))
        RecipeItem.objects.create(dish=self.dish_plov, product=self.rice, quantity=Decimal("200"))

        self.dish_tea = Dish.objects.create(category=self.category, name="Чай", price=Decimal("10000"))

    def create_order(self, lines, order_type=Order.OrderType.DINE_IN):
        order = services.create_order(self.waiter, order_type, self.table if order_type == Order.OrderType.DINE_IN else None)
        for dish, qty in lines:
            OrderItem.objects.create(order=order, dish=dish, quantity=qty, status=OrderItem.Status.SERVED)
        return order

    # --- 1. ТЕСТЫ КАССОВЫХ СМЕН (P0-02) ---

    def test_shift_open_close_and_reconciliation(self):
        """Открытие смены, внесение, изъятие и закрытие со сверкой фактического остатка."""
        shift = services.open_shift(self.cashier, initial_cash=Decimal("200000"))
        self.assertTrue(shift.is_open)
        self.assertEqual(shift.initial_cash, Decimal("200000"))
        self.assertEqual(shift.expected_cash, Decimal("200000"))

        # Внесение наличных (размен)
        services.add_cash_movement(shift, CashMovement.Kind.DEPOSIT, "100000", "Дополнительный размен", self.cashier)
        self.assertEqual(shift.cash_deposits, Decimal("100000"))
        self.assertEqual(shift.expected_cash, Decimal("300000"))

        # Продажа за наличные
        order = self.create_order([(self.dish_plov, 2)])  # 100 000 сум
        services.pay_order(order, self.cashier, Order.PaymentMethod.CASH, Decimal("100000"), None)
        self.assertEqual(shift.cash_sales, Decimal("100000"))
        self.assertEqual(shift.expected_cash, Decimal("400000"))

        # Инкассация (изъятие)
        services.add_cash_movement(shift, CashMovement.Kind.WITHDRAWAL, "150000", "Инкассация", self.cashier)
        self.assertEqual(shift.cash_withdrawals, Decimal("150000"))
        self.assertEqual(shift.expected_cash, Decimal("250000"))

        # Закрытие смены с фактической суммой 245 000 сум (недостача 5 000 сум)
        closed_shift = services.close_shift(shift, Decimal("245000"), "Недостача при закрытии", self.cashier)
        self.assertEqual(closed_shift.status, CashShift.Status.CLOSED)
        self.assertEqual(closed_shift.actual_cash, Decimal("245000"))
        self.assertEqual(closed_shift.difference, Decimal("-5000"))

    def test_cannot_open_two_shifts_concurrently(self):
        """Нельзя открыть вторую смену, пока первая не закрыта."""
        services.open_shift(self.cashier, initial_cash=Decimal("100000"))
        with self.assertRaises(services.PaymentError):
            services.open_shift(self.cashier, initial_cash=Decimal("50000"))

    def test_cannot_withdraw_more_than_expected_cash(self):
        """Нельзя изъять из кассы больше, чем в ней фактически ожидается."""
        shift = services.open_shift(self.cashier, initial_cash=Decimal("50000"))
        with self.assertRaises(services.PaymentError):
            services.add_cash_movement(shift, CashMovement.Kind.WITHDRAWAL, "100000", "Слишком много", self.cashier)

    # --- 2. ТЕСТЫ СУЩНОСТИ ПЛАТЕЖА И СМЕШАННОЙ ОПЛАТЫ (P0-01) ---

    def test_payment_entity_created_on_pay_order(self):
        """При оплате заказа создается атомарная запись Payment с корректными суммами."""
        shift = services.open_shift(self.cashier, initial_cash=Decimal("0"))
        order = self.create_order([(self.dish_plov, 1), (self.dish_tea, 1)])  # 60 000 сум

        # Оплата: 40 000 картой, 20 000 наличными (купюра 50 000, сдача 30 000)
        services.pay_order(
            order, self.cashier, Order.PaymentMethod.MIXED,
            received=Decimal("50000"), card=Decimal("40000")
        )

        payment = Payment.objects.get(order=order)
        self.assertEqual(payment.shift, shift)
        self.assertEqual(payment.amount, Decimal("60000"))
        self.assertEqual(payment.card_amount, Decimal("40000"))
        self.assertEqual(payment.cash_amount, Decimal("20000"))
        self.assertEqual(payment.cash_received, Decimal("50000"))
        self.assertEqual(payment.change, Decimal("30000"))
        self.assertFalse(payment.is_refund)

        # Проверяем выручку смены
        self.assertEqual(shift.cash_sales, Decimal("20000"))
        self.assertEqual(shift.card_sales, Decimal("40000"))
        self.assertEqual(shift.total_revenue, Decimal("60000"))

    # --- 3. ТЕСТЫ ИДЕМПОТЕНТНОСТИ (P0-05) ---

    def test_payment_idempotency_prevents_duplicate_charge(self):
        """Повторный вызов pay_order с тем же idempotency_key не создает дублирующих платежей и списаний."""
        services.open_shift(self.cashier, initial_cash=Decimal("0"))
        order = self.create_order([(self.dish_plov, 1)])  # 50 000 сум
        key = str(uuid.uuid4())

        # Первый вызов
        paid_1 = services.pay_order(
            order, self.cashier, Order.PaymentMethod.CASH,
            received=Decimal("50000"), card=None, idempotency_key=key
        )
        self.assertEqual(paid_1.status, Order.Status.PAID)
        self.assertEqual(Payment.objects.filter(order=paid_1).count(), 1)
        meat_stock_after_first = Product.objects.get(pk=self.meat.pk).stock
        self.assertEqual(meat_stock_after_first, Decimal("850"))  # 1000 - 150

        # Повторный вызов с тем же ключом
        paid_2 = services.pay_order(
            order, self.cashier, Order.PaymentMethod.CASH,
            received=Decimal("50000"), card=None, idempotency_key=key
        )
        self.assertEqual(paid_2.id, paid_1.id)
        self.assertEqual(Payment.objects.filter(order=paid_1).count(), 1)
        # Склад не списан второй раз!
        meat_stock_after_second = Product.objects.get(pk=self.meat.pk).stock
        self.assertEqual(meat_stock_after_second, Decimal("850"))

    # --- 4. ТЕСТЫ ВОЗВРАТА ЗАКАЗА (P0-03) ---

    def test_refund_order_restores_stock_and_records_shift_refund(self):
        """Возврат оплаченного чека создает запись возврата, уменьшает кассу и восстанавливает остатки склада."""
        shift = services.open_shift(self.cashier, initial_cash=Decimal("100000"))
        order = self.create_order([(self.dish_plov, 2)])  # 100 000 сум (300 г мяса, 400 г риса)
        services.pay_order(order, self.cashier, Order.PaymentMethod.CASH, Decimal("100000"), None)

        self.assertEqual(Product.objects.get(pk=self.meat.pk).stock, Decimal("700"))
        self.assertEqual(shift.expected_cash, Decimal("200000"))

        # Оформляем возврат
        services.refund_order(order, self.cashier, "Отказ гостя: блюдо остыло", return_to_stock=True)

        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertIn("Отказ гостя", order.cancel_reason)

        # Проверяем запись платежа-возврата
        refund_payment = Payment.objects.filter(order=order, is_refund=True).first()
        self.assertIsNotNone(refund_payment)
        self.assertEqual(refund_payment.amount, Decimal("100000"))
        self.assertEqual(refund_payment.cash_amount, Decimal("100000"))

        # Проверяем кассовый баланс смены (ожидаемые наличные уменьшились на сумму возврата)
        self.assertEqual(shift.cash_refunds, Decimal("100000"))
        self.assertEqual(shift.expected_cash, Decimal("100000"))

        # Проверяем восстановление продуктов на складе
        self.assertEqual(Product.objects.get(pk=self.meat.pk).stock, Decimal("1000"))
        self.assertEqual(Product.objects.get(pk=self.rice.pk).stock, Decimal("2000"))

    def test_cannot_refund_unpaid_order(self):
        """Нельзя вернуть заказ, который еще не был оплачен."""
        order = self.create_order([(self.dish_tea, 1)])
        with self.assertRaises(services.PaymentError):
            services.refund_order(order, self.cashier, "Ошибка", return_to_stock=True)

    # --- 5. ТЕСТЫ ЭНДПОИНТОВ И ИНТЕРФЕЙСА (HTTP VIEWS) ---

    def test_shift_endpoints_lifecycle(self):
        """Проверка работы веб-эндпоинтов открытия смены, внесения и закрытия смены."""
        self.client.force_login(self.cashier)

        # Открытие смены через POST
        res_open = self.client.post("/shifts/open/", {"initial_cash": "150000"})
        self.assertEqual(res_open.status_code, 302)
        shift = CashShift.objects.filter(status=CashShift.Status.OPEN).first()
        self.assertIsNotNone(shift)
        self.assertEqual(shift.initial_cash, Decimal("150000"))

        # Внесение наличных через POST
        res_dep = self.client.post("/shifts/movement/", {
            "kind": "deposit",
            "amount": "50000",
            "reason": "Разменные монеты",
        })
        self.assertEqual(res_dep.status_code, 302)
        self.assertEqual(shift.cash_deposits, Decimal("50000"))

        # Закрытие смены через POST
        res_close = self.client.post("/shifts/close/", {
            "actual_cash": "200000",
            "comment": "Смена закрыта в ноль",
        })
        self.assertEqual(res_close.status_code, 302)
        shift.refresh_from_db()
        self.assertEqual(shift.status, CashShift.Status.CLOSED)
        self.assertEqual(shift.actual_cash, Decimal("200000"))
        self.assertEqual(shift.difference, Decimal("0"))

    def test_refund_endpoint(self):
        """Проверка эндпоинта возврата заказа."""
        self.client.force_login(self.cashier)
        order = self.create_order([(self.dish_plov, 1)])
        services.pay_order(order, self.cashier, Order.PaymentMethod.CASH, Decimal("50000"), None)

        res = self.client.post(f"/orders/{order.id}/refund/", {
            "reason": "Блюдо не понравилось гостю",
            "return_to_stock": "1",
            "pin": "7391",
        })
        self.assertEqual(res.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertTrue(Payment.objects.filter(order=order, is_refund=True).exists())

    def test_pay_view_idempotency_via_http(self):
        """Двойной POST-запрос с одинаковым idempotency_key не приводит к повторному платежу."""
        self.client.force_login(self.cashier)
        order = self.create_order([(self.dish_plov, 1)])
        key = str(uuid.uuid4())

        # Первый POST
        r1 = self.client.post(f"/orders/{order.id}/pay/", {
            "method": "cash",
            "received": "50000",
            "idempotency_key": key,
        })
        self.assertEqual(r1.status_code, 302)
        self.assertEqual(Payment.objects.filter(order=order).count(), 1)

        # Повторный POST с тем же idempotency_key
        r2 = self.client.post(f"/orders/{order.id}/pay/", {
            "method": "cash",
            "received": "50000",
            "idempotency_key": key,
        })
        self.assertEqual(r2.status_code, 302)
        self.assertEqual(Payment.objects.filter(order=order).count(), 1)


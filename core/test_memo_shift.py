from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from pos import services

from .context_processors import _MemoShift

User = get_user_model()


class MemoShiftTests(TestCase):
    """Итоги смены в шапке считаются один раз и совпадают со значениями модели."""

    def setUp(self):
        self.cashier = User.objects.create_user(username="kass_memo", password="x", role="cashier")
        self.shift = services.open_shift(self.cashier, 100000)

    def test_values_equal_the_model(self):
        memo = _MemoShift.wrap(self.shift)
        for name in ("cash_sales", "card_sales", "click_sales", "cash_refunds", "cash_deposits",
                     "expected_cash", "initial_cash", "id"):
            self.assertEqual(getattr(memo, name), getattr(self.shift, name), name)
        self.assertEqual(memo.cashier.pk, self.cashier.pk)

    def test_each_total_is_queried_once(self):
        memo = _MemoShift.wrap(self.shift)
        with CaptureQueriesContext(connection) as first:
            memo.expected_cash
        with CaptureQueriesContext(connection) as again:
            memo.expected_cash
            memo.cash_sales
        with CaptureQueriesContext(connection) as plain:
            self.shift.expected_cash
            self.shift.cash_sales
        self.assertEqual(len(again), 0)
        self.assertLess(len(first), len(plain))

    def test_no_shift_stays_none(self):
        self.assertIsNone(_MemoShift.wrap(None))

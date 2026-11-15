from datetime import datetime, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import CafeSettings
from pos import booking
from pos.models import Booking, Hall, Table

User = get_user_model()


class BookingTests(TestCase):
    """Онлайн-бронь: заявка гостя, подтверждение со столом без пересечений, показ на схеме в день брони."""

    def setUp(self):
        cache.clear()
        self.cafe = CafeSettings.get()
        self.cafe.booking_enabled = True
        self.cafe.save()
        hall = Hall.objects.create(name="Зал")
        self.small = Table.objects.create(hall=hall, number="1", seats=2)
        self.big = Table.objects.create(hall=hall, number="2", seats=6)
        self.waiter = User.objects.create_user(username="w_book", password="pass12345", role="waiter")
        self.owner = User.objects.create_user(username="o_book", password="pass12345", role="owner")
        self.cook = User.objects.create_user(username="k_book", password="pass12345", role="cook")
        self.tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()

    def data(self, **extra):
        data = {"name": "Азиз", "phone": "+998 90 123 45 67", "guests": "4", "date": self.tomorrow, "time": "19:00", "comment": ""}
        data.update(extra)
        return data

    def post(self, **extra):
        return self.client.post(reverse("book"), self.data(**extra))

    # ---------- публичная страница ----------

    def test_page_opens_without_login_and_off_by_default(self):
        self.assertEqual(self.client.get(reverse("book")).status_code, 200)
        self.cafe.booking_enabled = False
        self.cafe.save()
        self.assertEqual(self.client.get(reverse("book")).status_code, 404)

    def test_guest_creates_a_request(self):
        with mock.patch("core.telegram.send") as send:
            response = self.post()
        self.assertRedirects(response, reverse("book_done"))
        b = Booking.objects.get()
        self.assertEqual((b.status, b.guests, b.phone, b.table), ("new", 4, "901234567", None))
        send.assert_called_once()
        self.assertEqual(send.call_args[0][1], "orders")

    def test_new_request_does_not_take_a_table(self):
        self.post()
        day = datetime.fromisoformat(self.tomorrow).date()
        self.assertEqual(len(booking.available_tables(day, datetime.strptime("19:00", "%H:%M").time(), 2)), 2)

    def test_validation_errors(self):
        cases = [
            {"name": "A"}, {"phone": "123"}, {"guests": "0"}, {"guests": "99"}, {"guests": "abc"},
            {"date": "2020-01-01"}, {"date": "oops"}, {"time": "03:00"}, {"time": ""},
            {"date": (timezone.localdate() + timedelta(days=90)).isoformat()},
        ]
        for extra in cases:
            self.post(**extra)
        self.assertFalse(Booking.objects.exists())

    def test_party_larger_than_any_table_is_rejected(self):
        self.post(guests="10")
        self.assertFalse(Booking.objects.exists())

    def test_duplicate_request_is_rejected(self):
        self.post()
        self.post()
        self.assertEqual(Booking.objects.count(), 1)

    def test_honeypot_creates_nothing(self):
        self.client.post(reverse("book"), self.data(website="http://spam"))
        self.assertFalse(Booking.objects.exists())

    def test_ip_throttle(self):
        for i in range(7):
            self.post(phone=f"+998 90 000 00 {i:02d}", time=f"{10 + i}:00")
        self.assertEqual(Booking.objects.count(), 5)

    def test_close_to_now_is_rejected(self):
        now = timezone.localtime().replace(hour=15, minute=0)
        cafe = CafeSettings.get()
        with self.assertRaises(booking.BookingError):
            booking.create_request(cafe, self.data(date=now.date().isoformat(), time="15:30"), now=now + timedelta(minutes=20))

    # ---------- сотрудники ----------

    def confirm(self, item, table, user=None):
        self.client.force_login(user or self.waiter)
        return self.client.post(reverse("bookings"), {"booking_id": item.id, "action": "confirm", "table": table.id})

    def test_staff_confirms_with_table(self):
        self.post()
        item = Booking.objects.get()
        self.confirm(item, self.big)
        item.refresh_from_db()
        self.assertEqual((item.status, item.table), ("confirmed", self.big))

    def test_overlapping_booking_cannot_take_the_same_table(self):
        self.post(phone="+998 90 111 11 11")
        self.post(phone="+998 90 222 22 22", time="20:00")
        first, second = Booking.objects.order_by("id")
        self.confirm(first, self.big)
        self.confirm(second, self.big)
        second.refresh_from_db()
        self.assertEqual(second.status, "new")

    def test_far_apart_bookings_share_a_table(self):
        self.post(phone="+998 90 111 11 11", time="12:00")
        self.post(phone="+998 90 222 22 22", time="19:00")
        first, second = Booking.objects.order_by("id")
        self.confirm(first, self.big)
        self.confirm(second, self.big)
        second.refresh_from_db()
        self.assertEqual(second.status, "confirmed")

    def test_table_too_small_is_refused(self):
        self.post()
        item = Booking.objects.get()
        self.confirm(item, self.small)
        item.refresh_from_db()
        self.assertEqual(item.status, "new")

    def test_reject(self):
        self.post()
        item = Booking.objects.get()
        self.client.force_login(self.waiter)
        self.client.post(reverse("bookings"), {"booking_id": item.id, "action": "reject"})
        item.refresh_from_db()
        self.assertEqual(item.status, "rejected")

    def test_cook_cannot_open_bookings(self):
        self.client.force_login(self.cook)
        self.assertEqual(self.client.get(reverse("bookings")).status_code, 403)

    def test_page_lists_new_requests_for_waiter(self):
        self.post()
        self.client.force_login(self.waiter)
        html = self.client.get(reverse("bookings")).content.decode()
        self.assertIn("Азиз", html)
        self.assertIn("901234567", html)

    # ---------- схема столов ----------

    def make_confirmed(self, minutes_from_now, table=None):
        start = timezone.localtime() + timedelta(minutes=minutes_from_now)
        return Booking.objects.create(
            name="Азиз", phone="901234567", guests=4, date=start.date(), time=start.time().replace(second=0, microsecond=0),
            status="confirmed", table=table or self.big,
        )

    def test_confirmed_booking_appears_on_table_an_hour_before(self):
        item = self.make_confirmed(30)
        booking.sync_today()
        self.big.refresh_from_db()
        item.refresh_from_db()
        self.assertTrue(self.big.is_reserved)
        self.assertTrue(self.big.reserved_for.startswith("Азиз"))
        self.assertTrue(item.applied)

    def test_old_confirmed_booking_becomes_done(self):
        item = Booking.objects.create(name="Старая", phone="900000000", guests=2, date=timezone.localdate() - timedelta(days=2),
                                      time=datetime.strptime("19:00", "%H:%M").time(), status="confirmed", table=self.big)
        booking.sync_today()
        item.refresh_from_db()
        self.assertEqual(item.status, "done")

    def test_cancelling_shown_booking_frees_the_table(self):
        item = self.make_confirmed(30)
        booking.sync_today()
        booking.close(item, "cancelled", self.owner)
        self.big.refresh_from_db()
        self.assertFalse(self.big.is_reserved)

    def test_manual_reservation_is_not_overwritten(self):
        self.big.reserved_for = "Другой гость"
        self.big.save()
        item = self.make_confirmed(30)
        booking.sync_today()
        self.big.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(self.big.reserved_for, "Другой гость")
        self.assertFalse(item.applied)

    def test_floor_map_triggers_sync(self):
        self.make_confirmed(30)
        self.client.force_login(self.waiter)
        self.client.get(reverse("table_map"))
        self.big.refresh_from_db()
        self.assertTrue(self.big.is_reserved)

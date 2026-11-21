from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from pos.models import Hall, Table

LAYOUT = ("TASHQARI:1-20", "ZAL:1-13,25-27,35-37", "XONALAR:1-4,28-34", "TEPA:1-5")


class CreateTablesTests(TestCase):
    def run_cmd(self, *args, **kw):
        out = StringIO()
        call_command("create_tables", *args, stdout=out, **kw)
        return out.getvalue()

    def test_cafe_layout_from_photos(self):
        self.run_cmd(*LAYOUT)
        self.assertEqual(list(Hall.objects.order_by("sort_order").values_list("name", flat=True)),
                         ["TASHQARI", "ZAL", "XONALAR", "TEPA"])
        counts = {h.name: h.tables.count() for h in Hall.objects.all()}
        self.assertEqual(counts, {"TASHQARI": 20, "ZAL": 19, "XONALAR": 11, "TEPA": 5})
        zal = set(Table.objects.filter(hall__name="ZAL").values_list("number", flat=True))
        self.assertIn("37", zal)
        self.assertNotIn("20", zal)

    def test_running_twice_creates_nothing_new(self):
        self.run_cmd(*LAYOUT)
        out = self.run_cmd(*LAYOUT)
        self.assertEqual(Table.objects.count(), 55)
        self.assertIn("залов 0, столов 0", out)

    def test_adds_only_missing_tables_to_existing_hall(self):
        hall = Hall.objects.create(name="ZAL")
        Table.objects.create(hall=hall, number="1", seats=6)
        self.run_cmd("ZAL:1-3", seats=2)
        self.assertEqual(Table.objects.get(hall=hall, number="1").seats, 6)  # существующий не тронут
        self.assertEqual(Table.objects.filter(hall=hall).count(), 3)

    def test_dry_run_changes_nothing(self):
        out = self.run_cmd(*LAYOUT, dry_run=True)
        self.assertFalse(Hall.objects.exists())
        self.assertIn("Будет создано: залов 4, столов 55", out)

    def test_word_numbers_and_errors(self):
        self.run_cmd("VIP:V1,V2")
        self.assertEqual(Table.objects.count(), 2)
        for bad in ("ZAL", "ZAL:", "ZAL:5-1", "ZAL:1-9999", "ZAL:1 2"):
            with self.assertRaises(CommandError):
                self.run_cmd(bad)

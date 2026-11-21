"""Создаёт залы и столы одной командой, например как в кафе на фото:

    python manage.py create_tables "TASHQARI:1-20" "ZAL:1-13,25-27,35-37" "XONALAR:1-4,28-34" "TEPA:1-5"

Каждый аргумент — «Зал:номера». Номера через запятую, диапазоны через дефис. Уже существующие залы и столы
не меняются и не удаляются, поэтому команду можно запускать повторно. --seats задаёт число мест у новых столов.
"""
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pos.models import Hall, Table

MAX_TABLES = 500


def parse_numbers(text):
    """«1-13,25-27,35» -> ['1', ..., '13', '25', '26', '27', '35'] без повторов, в исходном порядке."""
    out = []
    for part in (p.strip() for p in text.split(",")):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                raise CommandError(f"Диапазон «{part}»: начало больше конца.")
            if b - a >= MAX_TABLES:
                raise CommandError(f"Диапазон «{part}» слишком большой.")
            out.extend(str(n) for n in range(a, b + 1))
        elif re.fullmatch(r"[\w\-]{1,20}", part):
            out.append(part)  # номер может быть и словом: «VIP», «T1»
        else:
            raise CommandError(f"Не понимаю номер стола «{part}».")
    seen = set()
    return [n for n in out if not (n in seen or seen.add(n))]


def parse_spec(spec):
    if ":" not in spec:
        raise CommandError(f"«{spec}»: нужно «Зал:номера», например «ZAL:1-12».")
    name, numbers = spec.split(":", 1)
    name = name.strip()
    if not name or len(name) > 100:
        raise CommandError(f"«{spec}»: укажите название зала (до 100 символов).")
    tables = parse_numbers(numbers)
    if not tables:
        raise CommandError(f"«{spec}»: нет ни одного номера стола.")
    return name, tables


class Command(BaseCommand):
    help = "Создаёт залы и столы по списку «Зал:1-20,25». Существующие не трогает."

    def add_arguments(self, parser):
        parser.add_argument("halls", nargs="+", help="«Зал:номера», например «ZAL:1-13,25-27»")
        parser.add_argument("--seats", type=int, default=4, help="мест у новых столов (по умолчанию 4)")
        parser.add_argument("--dry-run", action="store_true", help="только показать, что будет создано")

    def handle(self, *args, halls, seats, dry_run, **options):
        if not 1 <= seats <= 30:
            raise CommandError("--seats: от 1 до 30.")
        plan = [parse_spec(spec) for spec in halls]
        created_halls = created_tables = 0
        with transaction.atomic():
            start = (Hall.objects.order_by("-sort_order").values_list("sort_order", flat=True).first() or 0) + 1
            for offset, (name, numbers) in enumerate(plan):
                hall = Hall.objects.filter(name=name).first()
                if hall is None:
                    created_halls += 1
                    if not dry_run:
                        hall = Hall.objects.create(name=name, sort_order=start + offset)
                existing = set(hall.tables.values_list("number", flat=True)) if hall and hall.pk else set()
                new = [n for n in numbers if n not in existing]
                created_tables += len(new)
                if not dry_run and new:
                    Table.objects.bulk_create([Table(hall=hall, number=n, seats=seats) for n in new])
                self.stdout.write(f"{name}: столов {len(numbers)}, новых {len(new)}")
            if dry_run:
                transaction.set_rollback(True)
        verb = "Будет создано" if dry_run else "Создано"
        self.stdout.write(self.style.SUCCESS(f"{verb}: залов {created_halls}, столов {created_tables}."))

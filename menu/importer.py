"""Импорт меню из CSV или Excel (.xlsx). Работает на стандартной библиотеке, без openpyxl.

Колонки (первая строка с названиями, порядок любой): Категория, Название, Цена,
а также необязательные: Название (узб.), Время приготовления, Описание.
Повторный импорт безопасен: блюдо находится по категории и названию, у него обновляется цена.
"""
import csv
import io
import re
import zipfile
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

from django.db import transaction
from django.utils.translation import gettext as _

from .models import Category, Dish

MAX_BYTES = 1_000_000
MAX_ROWS = 2000
MAX_PRICE = Decimal(100_000_000)

# Допустимые названия колонок (в нижнем регистре) для каждого поля
HEADERS = {
    "category": {"категория", "category", "kategoriya", "bo'lim", "раздел"},
    "name": {"название", "блюдо", "name", "nomi", "taom", "наименование"},
    "price": {"цена", "price", "narxi", "narx", "стоимость"},
    "name_uz": {"название (узб.)", "название узб", "name_uz", "nomi (o'zbekcha)", "узб"},
    "prep": {"время", "время приготовления", "мин", "prep", "minutes", "tayyorlash"},
    "description": {"описание", "description", "tavsif"},
}
TEMPLATE_CSV = (
    "Категория;Название;Цена;Название (узб.);Время приготовления;Описание\r\n"
    "Горячее;Плов;45000;Palov;18;Говядина, рис, морковь\r\n"
    "Напитки;Чай чёрный;8000;Qora choy;3;\r\n"
)


class ImportProblem(Exception):
    """Файл нельзя прочитать целиком: понятное сообщение для пользователя."""


def _clean(value):
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def _read_csv(data):
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ImportProblem(_("Не удалось прочитать файл. Сохраните его как CSV в кодировке UTF-8."))
    sample = text[:4096]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _col_index(ref):
    """'C5' -> 2"""
    letters = re.match(r"[A-Z]+", ref or "")
    number = 0
    for ch in letters.group(0) if letters else "A":
        number = number * 26 + ord(ch) - 64
    return number - 1


def _read_xlsx(data):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        names = archive.namelist()
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")) for si in root.findall("m:si", ns)]
        sheet = next(n for n in sorted(names) if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        root = ElementTree.fromstring(archive.read(sheet))
        rows = []
        for row in root.iter(f"{{{ns['m']}}}row"):
            cells = {}
            for cell in row.findall("m:c", ns):
                kind = cell.get("t")
                if kind == "inlineStr":
                    value = "".join(t.text or "" for t in cell.iter(f"{{{ns['m']}}}t"))
                else:
                    node = cell.find("m:v", ns)
                    value = node.text if node is not None and node.text is not None else ""
                    if kind == "s" and value.isdigit() and int(value) < len(shared):
                        value = shared[int(value)]
                cells[_col_index(cell.get("r"))] = value
            width = max(cells, default=-1) + 1
            rows.append([cells.get(i, "") for i in range(width)])
        return rows
    except (zipfile.BadZipFile, StopIteration, ElementTree.ParseError, KeyError):
        raise ImportProblem(_("Не удалось прочитать Excel-файл. Сохраните таблицу как .xlsx или CSV."))


def read_rows(uploaded):
    """Строки таблицы из загруженного файла."""
    data = uploaded.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ImportProblem(_("Файл больше 1 МБ. Разделите меню на несколько файлов."))
    if data[:2] == b"PK":
        rows = _read_xlsx(data)
    else:
        rows = _read_csv(data)
    rows = [r for r in rows if any(_clean(c) for c in r)]
    if not rows:
        raise ImportProblem(_("В файле нет строк."))
    if len(rows) > MAX_ROWS + 1:
        raise ImportProblem(_("В файле больше %(n)s строк. Разделите меню на части.") % {"n": MAX_ROWS})
    return rows


def _mapping(header):
    mapping = {}
    for index, title in enumerate(header):
        key = _clean(title).lower()
        for field, names in HEADERS.items():
            if key in names and field not in mapping:
                mapping[field] = index
    return mapping


def parse_price(raw):
    text = _clean(raw).replace(" ", "").replace(" ", "")
    text = re.sub(r"(сум|so'm|som|uzs)$", "", text, flags=re.I)
    if text.count(",") and not text.count("."):
        text = text.replace(",", ".")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value.quantize(Decimal(1)) if 0 < value <= MAX_PRICE else None


def import_rows(rows):
    """Создаёт категории и блюда. Возвращает {created, updated, skipped, errors:[(строка, причина)]}."""
    header = rows[0]
    mapping = _mapping(header)
    start = 1
    if not {"name", "price"} <= set(mapping):
        # Заголовка нет: считаем, что колонки идут как в образце — категория, название, цена
        mapping = {"category": 0, "name": 1, "price": 2}
        start = 0 if parse_price(header[2] if len(header) > 2 else "") else 1
    report = {"created": 0, "updated": 0, "skipped": 0, "errors": []}

    def cell(row, field):
        index = mapping.get(field)
        return _clean(row[index]) if index is not None and index < len(row) else ""

    with transaction.atomic():
        categories = {c.name.lower(): c for c in Category.objects.all()}
        # SQLite сравнивает без учёта регистра только латиницу, поэтому ищем существующие блюда сами
        existing = {(d.category_id, d.name.lower()): d for d in Dish.objects.all()}
        for line, row in enumerate(rows[start:], start=start + 1):
            name, price = cell(row, "name"), parse_price(cell(row, "price"))
            category_name = cell(row, "category") or _("Меню")
            if not name:
                report["skipped"] += 1
                report["errors"].append((line, _("нет названия блюда")))
                continue
            if price is None:
                report["skipped"] += 1
                report["errors"].append((line, _("«%(name)s»: цена не число или не больше нуля") % {"name": name[:40]}))
                continue
            category = categories.get(category_name.lower())
            if category is None:
                category = Category.objects.create(name=category_name[:100], sort_order=len(categories))
                categories[category_name.lower()] = category
            prep = cell(row, "prep")
            fields = {
                "price": price,
                "name_uz": cell(row, "name_uz")[:150],
                "description": cell(row, "description"),
                "prep_minutes": int(prep) if prep.isdigit() and int(prep) <= 600 else 0,
            }
            dish = existing.get((category.id, name[:150].lower()))
            if dish is None:
                created = Dish.objects.create(category=category, name=name[:150], **fields)
                existing[(category.id, created.name.lower())] = created
                report["created"] += 1
            else:
                changed = False
                for key, value in fields.items():
                    if value and getattr(dish, key) != value:
                        setattr(dish, key, value)
                        changed = True
                if changed:
                    dish.save()
                    report["updated"] += 1
                else:
                    report["skipped"] += 1
    report["errors"] = report["errors"][:20]
    return report

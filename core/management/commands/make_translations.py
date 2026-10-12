"""Сбор строк для перевода из шаблонов и кода в locale/uz/LC_MESSAGES/django.po.

Заменяет makemessages, которому нужен GNU gettext. Уже сделанные переводы сохраняются,
новые строки добавляются с пустым переводом. Исходный язык строк — русский.
"""
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from .compile_translations import read_po

# Строки в коде написаны по-русски. Каталог ru нужен всё равно: без него Django подставит
# перевод языка по умолчанию (uz) вместо исходной русской строки.
SOURCE_LANGUAGE = "ru"
APPS = ["accounts", "core", "inventory", "kitchen", "menu", "pos", "reports"]
# Значения, которые переводятся из переменной ({% translate group %}): названия групп модификаторов
EXTRA = ["Размер", "Вкус", "Порция"]

TRANSLATE_RE = re.compile(r"""\{%\s*translate\s+(?:"([^"]*)"|'([^']*)')""")
BLOCK_RE = re.compile(r"\{%\s*blocktranslate(?P<args>[^%]*)%\}(?P<body>.*?)\{%\s*endblocktranslate\s*%\}", re.S)
VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
TEMPLATE_CALL_RE = re.compile(r"""_\(\s*"([^"]*)"\s*\)""")
PY_RE = re.compile(r"""\b(?:_|gettext_lazy|gettext|pgettext)\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*[,)]""")


def _template_strings(text):
    found = []
    for m in TRANSLATE_RE.finditer(text):
        found.append(m.group(1) if m.group(1) is not None else m.group(2))
    for m in BLOCK_RE.finditer(text):
        body = m.group("body").replace("%", "%%")
        body = VAR_RE.sub(lambda v: f"%({v.group(1)})s", body)
        if "trimmed" in m.group("args"):
            body = re.sub(r"\s*\n\s*", " ", body.strip())
        found.append(body)
    found += TEMPLATE_CALL_RE.findall(text)
    return found


def _python_strings(text):
    found = []
    for m in PY_RE.finditer(text):
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        found.append(raw.encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8"))
    return found


def _quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def collect(base):
    strings = []
    for path in sorted((base / "templates").rglob("*.html")):
        strings += _template_strings(path.read_text(encoding="utf-8"))
    for app in APPS:
        for path in sorted((base / app).rglob("*.py")):
            if "migrations" in path.parts or path.name.startswith("test"):
                continue
            strings += _python_strings(path.read_text(encoding="utf-8"))
    seen, ordered = set(), []
    for s in strings + EXTRA:
        if s and s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered


class Command(BaseCommand):
    help = "Обновляет locale/<язык>/LC_MESSAGES/django.po: добавляет новые строки, сохраняет переводы."

    def add_arguments(self, parser):
        parser.add_argument("--locale", default="uz", help="uz (перевод) или ru (исходный язык)")

    def handle(self, *args, **options):
        base = Path(settings.BASE_DIR)
        po_path = base / "locale" / options["locale"] / "LC_MESSAGES" / "django.po"
        po_path.parent.mkdir(parents=True, exist_ok=True)
        existing = read_po(po_path) if po_path.exists() else {}
        strings = collect(base)
        lines = [
            'msgid ""',
            'msgstr ""',
            '"Content-Type: text/plain; charset=UTF-8\\n"',
            f'"Language: {options["locale"]}\\n"',
            '"Plural-Forms: nplurals=1; plural=0;\\n"',
            "",
        ]
        missing = 0
        for s in strings:
            translation = s if options["locale"] == SOURCE_LANGUAGE else existing.get(s, "")
            if not translation:
                missing += 1
            if "%(" in s:
                lines.append("#, python-format")
            lines += [f"msgid {_quote(s)}", f"msgstr {_quote(translation)}", ""]
        po_path.write_text("\n".join(lines), encoding="utf-8")
        self.stdout.write(f"{po_path}: строк {len(strings)}, без перевода {missing}")

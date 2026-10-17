"""Инструменты переводов без внешнего gettext: сборщик строк, чтение и запись .po, сборка .mo.

Зачем: на Windows нет xgettext и msgfmt, а интерфейс должен быть целиком на узбекском или целиком на русском.
Исходный язык проекта русский, поэтому русский перевод не нужен, а узбекский обязан покрывать все строки.
"""
import ast
import re
import struct
from pathlib import Path

PY_FUNCS = {"_", "gettext", "gettext_lazy", "_l", "N_", "pgettext", "ugettext", "gettext_noop"}
SKIP_DIRS = {".venv", "venv", "__pycache__", "node_modules", "migrations", "static", "staticfiles", "locale", "media",
             "backups", ".git", ".claude"}

TRANSLATE_TAG = re.compile(
    r"""\{%-?\s*(?:translate|trans)\s+(?P<q>["'])(?P<text>.*?)(?P=q)[^%]*?%\}""", re.S
)
BLOCK_TAG = re.compile(
    r"""\{%-?\s*(?:blocktranslate|blocktrans)(?P<opts>[^%]*)%\}(?P<body>.*?)\{%-?\s*(?:endblocktranslate|endblocktrans)\s*-?%\}""",
    re.S,
)
VAR = re.compile(r"\{\{\s*([a-zA-Z_][\w.]*)\s*\}\}")


def _unescape_template_string(text):
    return text.replace("\\'", "'").replace('\\"', '"')


def _from_block(opts, body):
    """Текст blocktranslate так, как его видит gettext: {{ x }} превращается в %(x)s, % в %%, пробелы схлопнуты при trimmed."""
    text = VAR.sub(lambda m: "\0" + m.group(1).split(".")[-1] + "\1", body.replace("%", "%%"))
    text = re.sub("\0(\\w+)\1", r"%(\1)s", text)
    if "trimmed" in opts:
        text = " ".join(text.split())
    return text


def extract_template_strings(path):
    source = Path(path).read_text(encoding="utf-8", errors="replace")
    # Django удваивает % в строках {% translate %}: в каталоге такой знак записывается как %%
    found = {_unescape_template_string(m.group("text")).replace("%", "%%") for m in TRANSLATE_TAG.finditer(source)}
    for m in BLOCK_TAG.finditer(source):
        if "plural" in m.group("body"):
            continue
        found.add(_from_block(m.group("opts"), m.group("body")))
    return {s for s in found if s.strip()}


def extract_python_strings(path):
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name in PY_FUNCS and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            found.add(node.args[0].value)
    return found


def extract_all(root):
    """Все переводимые строки проекта: {строка: [файлы]}."""
    root = Path(root)
    result = {}
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts) or not path.is_file():
            continue
        if path.suffix == ".html":
            strings = extract_template_strings(path)
        elif path.suffix == ".py" and not path.name.startswith("test_") and path.name != "tests.py":
            strings = extract_python_strings(path)
        else:
            continue
        for s in strings:
            result.setdefault(s, []).append(str(path.relative_to(root)).replace("\\", "/"))
    return result


CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def find_hardcoded_template_text(root):
    """Русский текст в шаблонах вне тегов перевода: [(файл, строка, текст)]. Комментарии игнорируем."""
    root = Path(root)
    found = []
    for path in sorted((root / "templates").rglob("*.html")):
        if path.name == "500.html":
            continue  # страница ошибки без шаблонных тегов: оба языка в разметке, нужный показывает скрипт
        s = path.read_text(encoding="utf-8")

        def blank(m):
            return "\n" * m.group(0).count("\n")

        s = re.sub(r"\{#.*?#\}", blank, s, flags=re.S)
        s = re.sub(r"\{%\s*comment.*?endcomment\s*%\}", blank, s, flags=re.S)
        s = re.sub(r"<!--.*?-->", blank, s, flags=re.S)
        s = re.sub(r"/\*.*?\*/", blank, s, flags=re.S)
        s = re.sub(r"(?m)(^|\s)//[^\n]*", r"\1", s)  # комментарии в скриптах
        s = re.sub(r"\{%-?\s*blocktrans(?:late)?.*?endblocktrans(?:late)?\s*-?%\}", blank, s, flags=re.S)
        s = re.sub(r"\{%-?\s*trans(?:late)?\s+(['\"]).*?\1[^%]*%\}", "", s, flags=re.S)
        s = re.sub(r"\{\{.*?\}\}", "", s, flags=re.S)
        s = re.sub(r"\{%.*?%\}", blank, s, flags=re.S)
        for number, line in enumerate(s.splitlines(), 1):
            if CYRILLIC.search(line):
                found.append((str(path.relative_to(root)).replace("\\", "/"), number, line.strip()[:100]))
    return found


# ---------- .po ----------

def _unquote(line):
    return ast.literal_eval(line)


def parse_po(path):
    """{msgid: msgstr} без заголовка. Понимает многострочные значения."""
    entries, msgid, msgstr, mode = {}, None, None, None
    text = Path(path).read_text(encoding="utf-8")
    for raw in text.splitlines() + [""]:
        line = raw.strip()
        if line.startswith("msgid "):
            if msgid is not None:
                entries[msgid] = msgstr or ""
            msgid, msgstr, mode = _unquote(line[6:]), "", "id"
        elif line.startswith("msgstr "):
            msgstr, mode = _unquote(line[7:]), "str"
        elif line.startswith('"'):
            if mode == "id":
                msgid += _unquote(line)
            elif mode == "str":
                msgstr += _unquote(line)
        elif not line and msgid is not None:
            entries[msgid] = msgstr or ""
            msgid, msgstr, mode = None, None, None
    entries.pop("", None)
    return entries


HEADER = (
    'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n"Language: {lang}\\n"\n'
    '"Plural-Forms: nplurals=1; plural=0;\\n"\n'
)


def _quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def write_po(path, entries, lang):
    parts = [HEADER.format(lang=lang)]
    for msgid, msgstr in entries.items():
        parts.append(f"msgid {_quote(msgid)}\nmsgstr {_quote(msgstr)}\n")
    Path(path).write_text("\n".join(parts), encoding="utf-8")


# ---------- .mo ----------

def compile_mo(entries, lang, path):
    """Бинарный файл переводов. Пустые переводы пропускаем: Django тогда покажет исходную строку."""
    header = f"Content-Type: text/plain; charset=UTF-8\nLanguage: {lang}\nPlural-Forms: nplurals=1; plural=0;\n"
    items = {"": header, **{k: v for k, v in entries.items() if v}}
    keys = sorted(items)
    ids, strs, offsets = b"", b"", []
    for key in keys:
        kid, kstr = key.encode("utf-8"), items[key].encode("utf-8")
        offsets.append((len(ids), len(kid), len(strs), len(kstr)))
        ids += kid + b"\0"
        strs += kstr + b"\0"
    count = len(keys)
    keystart = 7 * 4 + 16 * count
    valuestart = keystart + len(ids)
    table = b"".join(struct.pack("<II", l1, o1 + keystart) for o1, l1, _o2, _l2 in offsets)
    values = b"".join(struct.pack("<II", l2, o2 + valuestart) for _o1, _l1, o2, l2 in offsets)
    output = struct.pack("<Iiiiiii", 0x950412DE, 0, count, 7 * 4, 7 * 4 + count * 8, 0, 0)
    Path(path).write_bytes(output + table + values + ids + strs)

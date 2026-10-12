"""Компиляция locale/*/LC_MESSAGES/django.po в .mo без GNU gettext (msgfmt на Windows обычно нет)."""
import ast
import struct
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


def read_po(path):
    """Простой разбор .po: пары msgid -> msgstr (без множественных форм и контекстов)."""
    entries = {}
    msgid = msgstr = None
    target = None
    fuzzy = False

    def flush():
        if msgid is not None and msgstr is not None and not fuzzy:
            entries[msgid] = msgstr

    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#,") and "fuzzy" in line:
            fuzzy = True
        elif line.startswith("msgid "):
            flush()
            msgid, msgstr, target = ast.literal_eval(line[6:]), None, "id"
        elif line.startswith("msgstr "):
            msgstr, target = ast.literal_eval(line[7:]), "str"
        elif line.startswith('"') and target:
            if target == "id":
                msgid += ast.literal_eval(line)
            else:
                msgstr += ast.literal_eval(line)
        elif not line:
            flush()
            msgid = msgstr = target = None
            fuzzy = False
    flush()
    return entries


def write_mo(entries, path):
    """Формат GNU .mo: заголовок, таблицы смещений, строки. Пустые переводы пропускаются."""
    items = sorted((k, v) for k, v in entries.items() if v or k == "")
    ids = b""
    strs = b""
    offsets = []
    for key, value in items:
        k, v = key.encode("utf-8"), value.encode("utf-8")
        offsets.append((len(ids), len(k), len(strs), len(v)))
        ids += k + b"\0"
        strs += v + b"\0"
    count = len(items)
    key_start = 7 * 4 + count * 16
    value_start = key_start + len(ids)
    key_table, value_table = [], []
    for id_off, id_len, str_off, str_len in offsets:
        key_table += [id_len, key_start + id_off]
        value_table += [str_len, value_start + str_off]
    header = struct.pack("Iiiiiii", 0x950412DE, 0, count, 7 * 4, 7 * 4 + count * 8, 0, 0)
    data = header + struct.pack(f"{len(key_table)}i", *key_table) + struct.pack(f"{len(value_table)}i", *value_table)
    Path(path).write_bytes(data + ids + strs)
    return count


class Command(BaseCommand):
    help = "Компилирует переводы .po в .mo (без внешних программ)."

    def handle(self, *args, **options):
        for locale_dir in settings.LOCALE_PATHS:
            for po in Path(locale_dir).glob("*/LC_MESSAGES/*.po"):
                count = write_mo(read_po(po), po.with_suffix(".mo"))
                self.stdout.write(f"{po}: {count} строк")

"""Сборка данных для термопринтера ESC/POS.

Используются только базовые команды из стандарта Epson ESC/POS, которые есть у принтеров
этого класса: ESC @, ESC t, ESC a, ESC E, GS !, LF и GS V (отрезка). Растровая графика и QR-коды
не используются: их поддержка зависит от прошивки.
"""
import textwrap

ESC = b"\x1b"
GS = b"\x1d"

INIT = ESC + b"@"
ALIGN = {"left": 0, "center": 1, "right": 2}
# GS ! n: биты 0-3 высота, 4-7 ширина. Двойная ширина вдвое уменьшает символов в строке.
SIZE_NORMAL, SIZE_TALL, SIZE_BIG = 0x00, 0x01, 0x11
# GS V 66 n: подача бумаги до линии отреза и частичная отрезка
CUT = GS + b"V" + bytes([66, 0])

# Символы, которых нет в однобайтных кодировках принтера
REPLACE = {
    "ʻ": "'", "ʼ": "'", "‘": "'", "’": "'", "“": '"', "”": '"',
    "«": '"', "»": '"', "—": "-", "–": "-", "−": "-", "×": "x",
    " ": " ", " ": " ", " ": " ", "…": "...",
}


def clean(text):
    text = str(text)
    for src, dst in REPLACE.items():
        text = text.replace(src, dst)
    return text


class Ticket:
    """Чековая лента: строки текста с выравниванием и стилем, в конце подача и отрезка."""

    def __init__(self, width=48, encoding="cp866", codepage=17):
        self.width = max(16, min(int(width), 80))
        self.encoding = encoding
        self.buf = bytearray(INIT + ESC + b"t" + bytes([codepage & 0xFF]))

    def _encode(self, text):
        return clean(text).encode(self.encoding, errors="replace")

    def _raw(self, data):
        self.buf += data

    def text(self, line="", align="left", bold=False, size=SIZE_NORMAL):
        """Одна или несколько строк; длинный текст переносится по словам."""
        width = self.width // 2 if size & 0xF0 else self.width
        self._raw(ESC + b"a" + bytes([ALIGN[align]]))
        self._raw(ESC + b"E" + bytes([1 if bold else 0]))
        self._raw(GS + b"!" + bytes([size]))
        for part in textwrap.wrap(clean(line), width) or [""]:
            self._raw(self._encode(part) + b"\n")
        self._raw(GS + b"!" + bytes([SIZE_NORMAL]) + ESC + b"E\x00" + ESC + b"a\x00")
        return self

    def pair(self, left, right, bold=False):
        """Слева название, справа сумма. Если не помещается, название переносится, сумма в последней строке."""
        left, right = clean(left), clean(right)
        room = self.width - len(right) - 1
        lines = textwrap.wrap(left, max(room, 8)) or [""]
        self._raw(ESC + b"E" + bytes([1 if bold else 0]))
        for line in lines[:-1]:
            self._raw(self._encode(line) + b"\n")
        last = lines[-1]
        gap = self.width - len(last) - len(right)
        if gap < 1:
            self._raw(self._encode(last) + b"\n")
            last, gap = "", self.width - len(right)
        self._raw(self._encode(last + " " * gap + right) + b"\n")
        self._raw(ESC + b"E\x00")
        return self

    def rule(self, char="-"):
        self._raw(self._encode(char * self.width) + b"\n")
        return self

    def feed(self, lines=1):
        self._raw(b"\n" * lines)
        return self

    def finish(self, cut=True):
        """Подача ленты и отрезка. Без отрезки — запас бумаги, чтобы оторвать рукой."""
        self.feed(1 if cut else 4)
        if cut:
            self._raw(CUT)
        return bytes(self.buf)

"""Минимальный генератор .xlsx на стандартной библиотеке: несколько листов, жирная шапка, ширина колонок.
Нужен, чтобы выгружать отчёты в Excel без новых зависимостей."""
import io
import zipfile
from datetime import date, datetime
from decimal import Decimal
from xml.sax.saxutils import escape

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    "{sheets}</Types>"
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)
# 0 — обычный, 1 — жирная шапка на сером фоне, 2 — число с разделителями тысяч, 3 — дата и время
_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="1"><numFmt numFmtId="164" formatCode="dd.mm.yyyy\\ hh:mm"/></numFmts>'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFEFE8DC"/></patternFill></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="4">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    '<xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    "</cellXfs></styleSheet>"
)


def _col(n):
    name = ""
    while n:
        n, r = divmod(n - 1, 26)
        name = chr(65 + r) + name
    return name


def _serial(value):
    """Дата в числе Excel (дни от 1899-12-30)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone().replace(tzinfo=None)
        delta = value - datetime(1899, 12, 30)
    else:
        delta = datetime(value.year, value.month, value.day) - datetime(1899, 12, 30)
    return delta.days + delta.seconds / 86400


def _cell(ref, value, header=False):
    if header:
        return f'<c r="{ref}" t="inlineStr" s="1"><is><t>{escape(str(value))}</t></is></c>'
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return f'<c r="{ref}" t="inlineStr"><is><t>{"да" if value else "нет"}</t></is></c>'
    if isinstance(value, (int, float, Decimal)):
        return f'<c r="{ref}" s="2"><v>{value}</v></c>'
    if isinstance(value, (datetime, date)):
        return f'<c r="{ref}" s="3"><v>{_serial(value)}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'


def _sheet(rows):
    """rows: список строк, первая — шапка."""
    widths = {}
    out = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row, start=1):
            cells.append(_cell(f"{_col(c)}{r}", value, header=(r == 1)))
            size = len(str(value)) if value is not None else 0
            widths[c] = min(max(widths.get(c, 8), size + 2), 60)
        out.append(f'<row r="{r}">{"".join(cells)}</row>')
    cols = "".join(f'<col min="{c}" max="{c}" width="{w}" customWidth="1"/>' for c, w in sorted(widths.items()))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        f"<cols>{cols}</cols><sheetData>{''.join(out)}</sheetData></worksheet>"
    )


def _safe_name(name, index):
    for bad, good in (("/", "-"), ("\\", "-"), ("*", ""), ("?", ""), (":", ""), ("[", "("), ("]", ")")):
        name = name.replace(bad, good)
    return name[:31] or f"Sheet{index}"


def build_workbook(sheets):
    """sheets: список (название листа, строки). Возвращает байты .xlsx."""
    sheets = [(_safe_name(name, i), rows) for i, (name, rows) in enumerate(sheets, start=1)]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        overrides = "".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(1, len(sheets) + 1)
        )
        z.writestr("[Content_Types].xml", _CONTENT_TYPES.format(sheets=overrides))
        z.writestr("_rels/.rels", _ROOT_RELS)
        entries = "".join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, (name, _r) in enumerate(sheets, start=1))
        z.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{entries}</sheets></workbook>",
        )
        rels = "".join(
            f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(sheets) + 1)
        )
        n = len(sheets) + 1
        rels += f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>',
        )
        z.writestr("xl/styles.xml", _STYLES)
        for i, (_name, rows) in enumerate(sheets, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet(rows))
    return buf.getvalue()

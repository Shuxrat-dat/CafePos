"""Что печатается на ленте: чек, пречек, заказ для кухни и тестовая страница.

Содержание чека повторяет экранный чек (templates/pos/receipt.html), подписи берутся из тех же
строк перевода. Язык печати — язык заведения по умолчанию из настроек.
"""
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from core.models import CafeSettings
from core.templatetags.ui import num, tr, vat_part, weight

from .escpos import SIZE_BIG, SIZE_TALL, Ticket

RULER = "1234567890"


def _money(value):
    return f"{num(value)} {_('сум')}"


def _when(value):
    return timezone.localtime(value).strftime("%d.%m.%Y %H:%M")


def _person(user):
    return (user.get_full_name() or user.username) if user else ""


def _ticket(printer):
    return Ticket(printer.line_width, printer.encoding, printer.codepage)


def _items(order):
    return list(order.items.select_related("dish").prefetch_related("modifiers").order_by("id"))


def receipt(order, printer, precheck=False):
    cafe = CafeSettings.get()
    with translation.override(cafe.default_language):
        t = _ticket(printer)
        t.text(cafe.name, align="center", bold=True, size=SIZE_TALL)
        if cafe.address:
            t.text(cafe.address, align="center")
        if cafe.phone:
            t.text(f"{_('Тел.')}: {cafe.phone}", align="center")
        if cafe.receipt_header:
            t.text(cafe.receipt_header, align="center")
        t.rule()
        t.text((_("Пречек") if precheck else _("Кассовый чек")).upper(), align="center", bold=True)
        if precheck:
            t.text(_("Не является документом об оплате"), align="center")
        t.pair(_("Заказ"), f"№{order.id}")
        t.pair(_("Дата"), _when(timezone.now() if precheck or not order.closed_at else order.closed_at))
        t.pair(_("Тип"), str(order.place_label))
        if order.waiter_id:
            t.pair(_("Официант"), _person(order.waiter))
        if order.cashier_id:
            t.pair(_("Кассир"), _person(order.cashier))
        if order.customer_name or order.customer_phone:
            t.pair(_("Клиент"), f"{order.customer_name} {order.customer_phone}".strip())
        if order.delivery_address:
            t.pair(_("Адрес"), order.delivery_address)
        t.rule()

        items = _items(order)
        order._prefetched_objects_cache = {"items": items}
        for item in items:
            t.pair(tr(item.dish), num(item.line_total))
            if item.by_weight:
                detail = f"{weight(item.weight_grams)} x {num(item.price)} / {_('кг')}"
            else:
                detail = f"{item.quantity} x {num(item.unit_total)}"
            modifiers = ", ".join(tr(m) for m in item.modifiers.all())
            t.text(f"  {detail}" + (f", {modifiers}" if modifiers else ""))
        t.rule()

        totals = order.totals
        t.pair(_("Подытог"), num(totals["subtotal"]))
        if totals["discount"]:
            label = _("Скидка")
            if order.discount_kind == "percent":
                label += f" {num(order.discount_value)}%"
            t.pair(label, f"-{num(totals['discount'])}")
        if totals["service"]:
            t.pair(_("Обслуживание %(p)s%%") % {"p": num(order.service_percent)}, num(totals["service"]))
        t.pair(_("ИТОГО"), _money(totals["total"]), bold=True)
        if cafe.vat_percent:
            t.pair(_("в т.ч. НДС %(p)s%%") % {"p": num(cafe.vat_percent)}, num(vat_part(totals["total"], cafe.vat_percent)))
        if not precheck:
            t.pair(_("Оплата"), str(order.get_payment_method_display()))
            if order.payment_method == "mixed":
                t.pair(_("Картой"), num(order.card_amount))
            if order.change is not None:
                t.pair(_("Наличными получено"), num(order.cash_received))
                t.pair(_("Сдача"), _money(order.change), bold=True)
            entry = order.loyalty_entries.filter(points__gt=0).select_related("customer").first()
            if entry:
                t.rule()
                t.text(_("Начислено баллов: +%(p)s") % {"p": entry.points}, align="center")
                t.text(_("Ваш баланс: %(t)s") % {"t": entry.customer.points}, align="center", bold=True)
        t.feed()
        t.text(cafe.receipt_footer or _("Спасибо за визит!"), align="center")
        return t.finish(printer.cut_paper)


def kitchen(order, items, printer, addition=False):
    """Бланк для кухни или бара: только блюда этого принтера, крупно, без цен."""
    cafe = CafeSettings.get()
    with translation.override(cafe.default_language):
        t = _ticket(printer)
        t.text(printer.name.upper(), align="center", bold=True)
        if addition:
            t.text(_("Дозаказ").upper(), align="center", bold=True, size=SIZE_TALL)
        t.text(f"№{order.id}", align="center", bold=True, size=SIZE_BIG)
        t.text(str(order.place_label), align="center", bold=True, size=SIZE_TALL)
        t.pair(_("Официант"), _person(order.waiter))
        t.pair(_("Время"), timezone.localtime().strftime("%H:%M"))
        t.rule()
        for item in items:
            amount = weight(item.weight_grams) if item.by_weight and item.weight_grams else f"{item.quantity} x"
            t.text(f"{amount} {tr(item.dish)}", bold=True, size=SIZE_TALL)
            for modifier in item.modifiers.all():
                t.text(f"   + {tr(modifier)}")
            if item.comment:
                t.text(f"   ! {item.comment}", bold=True)
        t.rule()
        return t.finish(printer.cut_paper)


def test_page(printer):
    cafe = CafeSettings.get()
    with translation.override(cafe.default_language):
        t = _ticket(printer)
        t.text(_("Тестовая печать"), align="center", bold=True, size=SIZE_TALL)
        t.text(cafe.name, align="center")
        t.rule()
        t.pair(_("Принтер"), printer.name)
        t.pair(_("Подключение"), printer.address)
        t.pair(_("Символов в строке"), str(printer.line_width))
        t.pair(_("Кодировка"), f"{printer.get_encoding_display()}, ESC t {printer.codepage}")
        t.rule()
        t.text("Русский: Съешь же ещё этих мягких французских булок")
        t.text("Oʻzbekcha: oʻ gʻ sh ch ng, Toshkent")
        t.text("0123456789  +-%=№")
        t.text((RULER * 10)[: printer.line_width])
        t.pair(_("Слева"), _("справа"))
        t.text(_("Крупно"), align="center", bold=True, size=SIZE_BIG)
        t.rule()
        t.text(_when(timezone.now()), align="center")
        return t.finish(printer.cut_paper)

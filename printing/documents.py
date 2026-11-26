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
        t.text(cafe.name, align="center", bold=True, size=SIZE_BIG)  # название заведения крупно
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
        for number, item in enumerate(items):
            if number:
                t.dashed()  # пунктир между блюдами
            # Количество в одной строке с названием: «2 x Плов ... 80 000»; на вес — «Рыба 1,5 кг ... 112 500»
            if item.by_weight:
                title = f"{tr(item.dish)} {weight(item.weight_grams, item.measure)}"
                detail = f"{num(item.price)} / {_('л') if item.measure == 'l' else _('кг')}"
            else:
                title = f"{item.quantity} x {tr(item.dish)}"
                detail = ""
            t.pair(title, num(item.line_total))
            modifiers = ", ".join(tr(m) for m in item.modifiers.all())
            takeaway = _("с собой, без обслуживания") if item.takeaway and order.service_percent else (_("с собой") if item.takeaway else "")
            extra = ", ".join(part for part in (detail, modifiers, takeaway) if part)
            if extra:
                t.text(f"  {extra}")
        t.rule()

        totals = order.totals
        t.pair(_("Подытог"), num(totals["subtotal"]))
        if order.room_amount:
            minutes = order.room_minutes or order.room_live()[0]
            t.pair(_("в т.ч. аренда кабины, %(m)s мин") % {"m": minutes}, num(order.room_amount))
        if totals["discount"]:
            label = _("Скидка")
            if order.discount_kind == "percent":
                label += f" {num(order.discount_value)}%"
            t.pair(label, f"-{num(totals['discount'])}")
        if totals["service"]:
            t.pair(_("Обслуживание %(p)s%%") % {"p": num(order.service_percent)}, num(totals["service"]))
        t.pair(_("ИТОГО"), _money(totals["total"]), bold=True, size=SIZE_BIG)  # общая сумма крупно
        if cafe.vat_percent:
            t.pair(_("в т.ч. НДС %(p)s%%") % {"p": num(cafe.vat_percent)}, num(vat_part(totals["total"], cafe.vat_percent)))
        if not precheck:
            t.pair(_("Оплата"), str(order.get_payment_method_display()))
            if order.payment_method == "mixed":
                if order.card_amount:
                    t.pair(_("Картой"), num(order.card_amount))
                if order.click_amount:
                    t.pair("Click", num(order.click_amount))
                if order.debt_amount:
                    t.pair(_("В долг"), num(order.debt_amount))
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


def _kitchen_cols(qty, price, total):
    """Колонки «Кол-во | Цена | Итого» фиксированной ширины (24 символа), справа от названия блюда."""
    return f"{qty:>6}{price:>9}{total:>9}"


def kitchen(order, items, printer, addition=False):
    """Бланк для кухни или бара: только блюда этого принтера.
    Крупно номер заказа, потом тип заказа, стол и официант, таблица «Наименование | Кол-во | Цена | Итого» и время."""
    cafe = CafeSettings.get()
    with translation.override(cafe.default_language):
        t = _ticket(printer)
        t.text(f"{_('Номер заказа')}: #{order.id}", align="center", bold=True, size=SIZE_BIG)
        if addition:
            t.text(_("Дозаказ").upper(), align="center", bold=True, size=SIZE_TALL)
        t.dashed()
        if order.is_dine_in:
            kind = _("На стол")
            if any(item.takeaway for item in items):
                kind += f" ({_('С собой')})"
        else:
            kind = str(order.get_order_type_display())
        t.pair(f"{_('Тип заказа')}:", kind)
        if order.is_dine_in and order.table_id:
            t.pair(_("Номер стола"), f"{order.table.number} ({tr(order.table.hall)})")
        if order.waiter_id:
            t.pair(_("Официант"), _person(order.waiter))
        # Заказ Juma: номер для покупателя и код выдачи водителю. Адрес покупателя на кухню не печатаем.
        juma = getattr(order, "juma", None)
        if juma is not None:
            t.text(f"Juma {juma.number}", align="center", bold=True)
            t.pair(_("Код выдачи"), str(juma.pickup_code), bold=True)
        t.dashed()
        t.pair(_("Наименование"), _kitchen_cols(_("Кол-во"), _("Цена"), _("Итого")), bold=True)
        for item in items:
            if item.by_weight:
                qty = weight(item.weight_grams, item.measure) if item.weight_grams else "?"
                price = num(item.price)
            else:
                qty, price = str(item.quantity), num(item.unit_total)
            total = num(item.line_total) if not item.by_weight or item.weight_grams else ""
            t.pair(tr(item.dish), _kitchen_cols(qty, price, total), bold=True)
            for modifier in item.modifiers.all():
                t.text(f"   + {tr(modifier)}")
            if item.comment:
                t.text(f"   ! {item.comment}", bold=True)
            if item.takeaway:
                t.text(f"   >> {_('С собой').upper()}", bold=True)
        t.dashed()
        t.text(f"{_('Время')} - {timezone.localtime().strftime('%H:%M %d.%m.%Y')}")
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

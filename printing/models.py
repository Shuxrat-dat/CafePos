"""Термопринтеры ESC/POS и очередь печати.

Касса может работать на сервере в интернете, а принтеры стоят в локальной сети кафе. Поэтому сервер
сам к принтеру не подключается: он складывает готовые ESC/POS-данные в очередь, а программа печати
на компьютере в кафе (print_agent/) забирает задания по токену и отправляет их на принтер.
"""
import secrets
from datetime import timedelta

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from menu.models import Category

AGENT_ONLINE_SECONDS = 30
# Как часто программа печати проверяет связь с принтерами и когда результат считается устаревшим
CHECK_INTERVAL_SECONDS = 30
CHECK_STALE_SECONDS = 120


class Printer(models.Model):
    class Connection(models.TextChoices):
        NETWORK = "network", _("Сеть (LAN): IP-адрес и порт")
        WINDOWS = "windows", _("USB: принтер, установленный в Windows")

    class Encoding(models.TextChoices):
        CP866 = "cp866", "CP866"
        CP1251 = "cp1251", "Windows-1251"

    name = models.CharField(_("Название"), max_length=60, help_text=_("Например: Касса, Кухня, Бар."))
    connection = models.CharField(_("Подключение"), max_length=10, choices=Connection.choices, default=Connection.NETWORK)
    host = models.CharField(_("IP-адрес принтера"), max_length=100, blank=True)
    port = models.PositiveIntegerField(_("Порт"), default=9100)
    windows_name = models.CharField(
        _("Имя принтера в Windows"), max_length=150, blank=True,
        help_text=_("Точно как в «Принтеры и сканеры». Список можно получить командой программы печати --list."),
    )
    prints_receipts = models.BooleanField(_("Печатает чеки и пречеки"), default=True)
    auto_receipt = models.BooleanField(_("Чек сразу после оплаты"), default=True)
    prints_kitchen = models.BooleanField(_("Печатает заказы для кухни"), default=False)
    categories = models.ManyToManyField(
        Category, verbose_name=_("Категории блюд для этого принтера"), blank=True,
        help_text=_("Сюда уходят только блюда отмеченных категорий. Ничего не отмечено — все блюда."),
    )
    copies = models.PositiveSmallIntegerField(_("Копий"), default=1)
    line_width = models.PositiveSmallIntegerField(
        _("Символов в строке"), default=48,
        help_text=_("Для ленты 80 мм обычно 48 (шрифт A) или 42. Для 58 мм — 32."),
    )
    encoding = models.CharField(_("Кодировка кириллицы"), max_length=10, choices=Encoding.choices, default=Encoding.CP866)
    codepage = models.PositiveSmallIntegerField(
        _("Кодовая страница (ESC t)"), default=17,
        help_text=_("Номер таблицы символов в принтере. По стандарту Epson: 17 — CP866, 46 — Windows-1251. "
                    "Если вместо русских букв печатаются знаки, сверьте номер со страницей самотестирования принтера."),
    )
    cut_paper = models.BooleanField(_("Отрезать бумагу"), default=True)
    is_active = models.BooleanField(_("Включён"), default=True)
    # Проверка связи: программа печати подключается к принтеру, ничего не печатая
    online = models.BooleanField(_("Связь есть"), null=True, blank=True)
    checked_at = models.DateTimeField(_("Связь проверена"), null=True, blank=True)
    check_code = models.CharField(max_length=30, blank=True)
    check_detail = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = "Принтер"
        verbose_name_plural = "Принтеры"
        ordering = ["name", "id"]

    def __str__(self):
        return self.name

    @property
    def address(self):
        if self.connection == self.Connection.NETWORK:
            return f"{self.host}:{self.port}"
        return self.windows_name

    def takes_dish(self, dish):
        """Кухонный принтер без отмеченных категорий получает все блюда."""
        ids = {c.id for c in self.categories.all()}
        return not ids or dish.category_id in ids

    @property
    def last_job(self):
        return self.jobs.order_by("-id").first()

    @property
    def connection_state(self):
        """'ok', 'error' или 'unknown' (не проверялся, данные устарели или программа печати не на связи)."""
        if self.online is None or not self.checked_at:
            return "unknown"
        if timezone.now() - self.checked_at > timedelta(seconds=CHECK_STALE_SECONDS):
            return "unknown"
        return "ok" if self.online else "error"

    @property
    def check_error(self):
        return error_text(self.check_code, self.check_detail) if self.check_code else ""


class PrintJob(models.Model):
    class Kind(models.TextChoices):
        RECEIPT = "receipt", _("Чек")
        PRECHECK = "precheck", _("Пречек")
        KITCHEN = "kitchen", _("Кухня")
        TEST = "test", _("Тест")

    class Status(models.TextChoices):
        PENDING = "pending", _("В очереди")
        SENT = "sent", _("Передано программе печати")
        DONE = "done", _("Напечатано")
        ERROR = "error", _("Ошибка")

    printer = models.ForeignKey(Printer, verbose_name=_("Принтер"), on_delete=models.CASCADE, related_name="jobs")
    kind = models.CharField(_("Что печатаем"), max_length=10, choices=Kind.choices)
    status = models.CharField(_("Статус"), max_length=10, choices=Status.choices, default=Status.PENDING)
    order = models.ForeignKey(
        "pos.Order", verbose_name=_("Заказ"), on_delete=models.SET_NULL, null=True, blank=True, related_name="print_jobs"
    )
    title = models.CharField(_("Задание"), max_length=100)
    data = models.BinaryField(_("Данные ESC/POS"))
    # Код ошибки от программы печати; текст для людей собирается при показе, на языке того, кто смотрит
    error_code = models.CharField(_("Ошибка"), max_length=30, blank=True)
    error_detail = models.CharField(_("Подробности ошибки"), max_length=200, blank=True)
    created_at = models.DateTimeField(_("Создано"), default=timezone.now)
    claimed_at = models.DateTimeField(_("Передано"), null=True, blank=True)
    finished_at = models.DateTimeField(_("Завершено"), null=True, blank=True)

    class Meta:
        verbose_name = "Задание печати"
        verbose_name_plural = "Задания печати"
        ordering = ["-id"]
        indexes = [models.Index(fields=["status", "id"])]

    def __str__(self):
        return f"{self.title} → {self.printer}"

    @property
    def error(self):
        return error_text(self.error_code, self.error_detail) if self.error_code else ""

    @property
    def label(self):
        """Название задания на языке того, кто смотрит (title хранит текст на момент создания)."""
        from django.utils.translation import gettext

        if self.kind == self.Kind.TEST or not self.order_id:
            return gettext("Тестовая печать") if self.kind == self.Kind.TEST else self.title
        names = {self.Kind.RECEIPT: "Чек №%(n)s", self.Kind.PRECHECK: "Пречек №%(n)s", self.Kind.KITCHEN: "Кухня №%(n)s"}
        return gettext(names[self.kind]) % {"n": self.order_id}


def error_text(code, detail=""):
    """Код ошибки печати -> понятный текст на текущем языке."""
    from django.utils.translation import gettext

    messages = {
        "timeout": gettext("Принтер %(d)s не отвечает. Проверьте, что он включён и в одной сети с компьютером."),
        "refused": gettext("Принтер %(d)s отклонил подключение. Проверьте порт (обычно 9100)."),
        "network": gettext("Нет связи с принтером: %(d)s."),
        "not_found": gettext("Принтер не найден в Windows: %(d)s. Проверьте имя в настройках."),
        "windows": gettext("Windows не смог отправить данные на принтер: %(d)s."),
        "no_pywin32": gettext("Для USB-принтера на компьютере нужен модуль pywin32: py -m pip install pywin32"),
        "bad_connection": gettext("Неизвестный тип подключения принтера."),
        "no_result": gettext("Программа печати не сообщила результат. Проверьте принтер и повторите."),
    }
    template = messages.get(code)
    if template is None:
        return gettext("Ошибка принтера: %(d)s") % {"d": f"{code} {detail}".strip()}
    return template % {"d": detail} if "%(d)s" in template else template


class PrintAgent(models.Model):
    """Программа печати в кафе: токен для входа и время последней связи. Одна запись на заведение."""

    token = models.CharField(_("Токен"), max_length=64)
    last_seen_at = models.DateTimeField(_("Последняя связь"), null=True, blank=True)
    info = models.CharField(_("Компьютер"), max_length=150, blank=True)
    check_requested_at = models.DateTimeField(null=True, blank=True)
    check_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Программа печати"
        verbose_name_plural = "Программа печати"

    @classmethod
    def get(cls):
        obj = cls.objects.order_by("id").first()
        if obj is None:
            obj = cls.objects.create(token=secrets.token_urlsafe(32))
        return obj

    def new_token(self):
        self.token = secrets.token_urlsafe(32)
        self.save(update_fields=["token"])

    @property
    def is_online(self):
        return bool(self.last_seen_at) and timezone.now() - self.last_seen_at < timedelta(seconds=AGENT_ONLINE_SECONDS)

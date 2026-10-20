import ipaddress
import re

from django import forms
from django.utils.translation import gettext_lazy as _

from core.forms import StyledModelForm
from menu.models import Category

from .models import Printer

HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*$")


class PrinterForm(StyledModelForm):
    class Meta:
        model = Printer
        fields = [
            "name", "connection", "host", "port", "windows_name", "prints_receipts", "auto_receipt",
            "prints_kitchen", "categories", "copies", "line_width", "encoding", "codepage", "cut_paper", "is_active",
        ]
        widgets = {"categories": forms.CheckboxSelectMultiple}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["categories"].queryset = Category.objects.filter(is_active=True)
        self.fields["host"].widget.attrs.update({"placeholder": "192.168.1.200", "inputmode": "decimal"})

    def clean_host(self):
        host = self.cleaned_data.get("host", "").strip()
        if not host:
            return host
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not HOSTNAME.match(host):
                raise forms.ValidationError(_("Укажите IP-адрес, например 192.168.1.200."))
        return host

    def clean_port(self):
        port = self.cleaned_data.get("port")
        if port is None or not 1 <= port <= 65535:
            raise forms.ValidationError(_("Порт — число от 1 до 65535. Для сетевых термопринтеров обычно 9100."))
        return port

    def clean_copies(self):
        copies = self.cleaned_data.get("copies") or 1
        if not 1 <= copies <= 5:
            raise forms.ValidationError(_("От 1 до 5 копий."))
        return copies

    def clean_line_width(self):
        width = self.cleaned_data.get("line_width") or 48
        if not 24 <= width <= 64:
            raise forms.ValidationError(_("От 24 до 64 символов."))
        return width

    def clean_codepage(self):
        page = self.cleaned_data.get("codepage")
        if page is None or not 0 <= page <= 255:
            raise forms.ValidationError(_("Номер от 0 до 255."))
        return page

    def clean(self):
        data = super().clean()
        connection = data.get("connection")
        if connection == Printer.Connection.NETWORK and not data.get("host"):
            self.add_error("host", _("Укажите IP-адрес принтера."))
        if connection == Printer.Connection.WINDOWS and not data.get("windows_name", "").strip():
            self.add_error("windows_name", _("Укажите имя принтера, как оно называется в Windows."))
        if not data.get("prints_receipts") and not data.get("prints_kitchen"):
            raise forms.ValidationError(_("Отметьте, что печатает принтер: чеки, заказы для кухни или и то и другое."))
        return data

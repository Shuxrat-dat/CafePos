from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.utils.translation import gettext_lazy as _

from pos.models import Hall, Table

from .models import CafeSettings

INPUT = (
    "w-full rounded-xl border border-line bg-white px-3.5 min-h-[46px] text-[15px] text-ink "
    "placeholder:text-muted focus:outline-none focus:ring-2 focus:ring-brand/30 focus:border-brand"
)
TEXTAREA = INPUT.replace("min-h-[46px]", "py-2.5")
CHECK = "w-5 h-5 rounded accent-[#0f766e]"


def style_form(form):
    """Единый вид полей ввода во всех формах."""
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple, forms.RadioSelect)):
            widget.attrs.setdefault("class", CHECK)
        elif isinstance(widget, forms.ClearableFileInput):
            widget.attrs.setdefault("class", "block w-full text-sm text-muted file:mr-3 file:rounded-lg file:border-0 file:bg-slate-100 file:px-3 file:py-2 file:text-ink")
        elif isinstance(widget, forms.Textarea):
            widget.attrs.setdefault("class", TEXTAREA)
        else:
            widget.attrs.setdefault("class", INPUT)
    return form


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_form(self)


class CafeSettingsForm(StyledModelForm):
    class Meta:
        model = CafeSettings
        fields = [
            "name", "logo", "address", "phone", "default_language", "service_percent", "vat_percent",
            "receipt_header", "receipt_footer", "accept_cash", "accept_card", "accept_mixed", "kitchen_warn_minutes",
        ]

    def clean(self):
        data = super().clean()
        if not (data.get("accept_cash") or data.get("accept_card") or data.get("accept_mixed")):
            raise forms.ValidationError(_("Оставьте хотя бы один способ оплаты."))
        return data


class HallForm(StyledModelForm):
    class Meta:
        model = Hall
        fields = ["name", "name_uz", "sort_order"]


class TableForm(StyledModelForm):
    class Meta:
        model = Table
        fields = ["hall", "number", "seats"]

    def clean_seats(self):
        seats = self.cleaned_data["seats"]
        if not 1 <= seats <= 30:
            raise forms.ValidationError(_("От 1 до 30 мест."))
        return seats


User = get_user_model()


class StaffForm(StyledModelForm):
    password = forms.CharField(
        label=_("Пароль"), required=False, widget=forms.PasswordInput(render_value=False),
        help_text=_("Для нового сотрудника обязателен. Для существующего — оставьте пустым, чтобы не менять."),
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "role", "is_active"]
        labels = {
            "username": _("Логин"),
            "first_name": _("Имя"),
            "last_name": _("Фамилия"),
            "role": _("Роль"),
            "is_active": _("Может входить"),
        }
        help_texts = {"username": ""}

    def __init__(self, *args, editor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.editor = editor
        roles = list(User.Role.choices)
        # Администратор не может назначать владельцев
        if editor is not None and not editor.is_superuser and editor.role != User.Role.OWNER:
            roles = [r for r in roles if r[0] != User.Role.OWNER]
        self.fields["role"].choices = roles

    def clean_password(self):
        password = self.cleaned_data.get("password", "")
        if not self.instance.pk and not password:
            raise forms.ValidationError(_("Задайте пароль."))
        if password:
            validate_password(password, self.instance)
        return password

    def clean_is_active(self):
        active = self.cleaned_data.get("is_active")
        if self.instance.pk and self.editor and self.instance.pk == self.editor.pk and not active:
            raise forms.ValidationError(_("Нельзя отключить собственную учётную запись."))
        return active

    def save(self, commit=True):
        user = super().save(commit=False)
        if self.cleaned_data.get("password"):
            user.set_password(self.cleaned_data["password"])
        if commit:
            user.save()
        return user

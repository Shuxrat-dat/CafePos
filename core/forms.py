import re

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


def localized_name(obj, field="name"):
    """Название из базы на языке интерфейса: для узбекского берём name_uz, если оно заполнено."""
    from django.utils.translation import get_language

    if get_language() == "uz":
        value = getattr(obj, f"{field}_uz", "")
        if value:
            return value
    return getattr(obj, field, str(obj))


def style_form(form):
    """Единый вид полей ввода во всех формах."""
    for field in form.fields.values():
        widget = field.widget
        if isinstance(field, forms.ModelChoiceField) and field.empty_label is not None:
            field.empty_label = _("Выберите…")  # иначе в списке остаётся английское «Select an option»
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
            "receipt_header", "receipt_footer", "accept_cash", "accept_card", "accept_click", "accept_mixed", "accept_debt", "kitchen_warn_minutes",
            "loyalty_percent", "cashier_max_discount", "cashier_sees_reports", "guest_auto_send",
            "booking_enabled", "booking_open", "booking_close", "booking_max_guests", "booking_days_ahead",
        ]
        widgets = {
            "booking_open": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "booking_close": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
        }

    def clean(self):
        data = super().clean()
        if not (data.get("accept_cash") or data.get("accept_card") or data.get("accept_click") or data.get("accept_mixed")):
            raise forms.ValidationError(_("Оставьте хотя бы один способ оплаты."))
        return data


class CafeBasicsForm(StyledModelForm):
    """Короткая форма для мастера быстрой настройки: только то, что нужно в первую минуту."""

    class Meta:
        model = CafeSettings
        fields = ["name", "address", "phone", "default_language"]

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError(_("Укажите название заведения."))
        return name


class TelegramSettingsForm(StyledModelForm):
    """Токен вводится один раз и дальше не показывается. Пустое поле оставляет прежний токен."""

    telegram_bot_token = forms.CharField(
        label=_("Токен Telegram-бота"), required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off", "placeholder": "123456:ABC-..."}),
        help_text=_("Создайте бота у @BotFather и вставьте выданный токен."),
    )

    class Meta:
        model = CafeSettings
        fields = [
            "telegram_bot_token", "telegram_chat_id", "tg_notify_shift", "tg_notify_cancel",
            "tg_notify_discount", "tg_notify_stock", "tg_notify_review", "tg_notify_orders", "tg_discount_percent",
        ]
        help_texts = {
            "telegram_chat_id": _("Номер чата или @имя канала. Напишите боту /start, затем узнайте номер у @userinfobot."),
        }

    def clean_telegram_bot_token(self):
        token = self.cleaned_data.get("telegram_bot_token", "").strip()
        if not token:
            return self.instance.telegram_bot_token  # оставляем сохранённый
        if not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,60}", token):
            raise forms.ValidationError(_("Токен выглядит неверно. Он состоит из цифр, двоеточия и букв."))
        return token

    def clean_telegram_chat_id(self):
        chat = self.cleaned_data.get("telegram_chat_id", "").strip()
        if chat and not re.fullmatch(r"-?\d{3,20}|@[A-Za-z][A-Za-z0-9_]{3,31}", chat):
            raise forms.ValidationError(_("Укажите номер чата (например, 123456789) или @имя канала."))
        return chat


class HallForm(StyledModelForm):
    class Meta:
        model = Hall
        fields = ["name", "name_uz", "sort_order", "service_percent"]


class TableForm(StyledModelForm):
    class Meta:
        model = Table
        fields = ["hall", "number", "seats", "hourly_price", "billing_step_minutes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["hall"].label_from_instance = localized_name

    def clean_hourly_price(self):
        value = self.cleaned_data.get("hourly_price") or 0
        if value < 0:
            raise forms.ValidationError(_("Цена не может быть меньше нуля."))
        return value

    def clean_billing_step_minutes(self):
        value = self.cleaned_data.get("billing_step_minutes") or 30
        if not 5 <= value <= 240:
            raise forms.ValidationError(_("Шаг расчёта: от 5 до 240 минут."))
        return value

    def clean_seats(self):
        seats = self.cleaned_data["seats"]
        if not 1 <= seats <= 30:
            raise forms.ValidationError(_("От 1 до 30 мест."))
        return seats


User = get_user_model()


class StaffForm(StyledModelForm):
    reset_cancel_pin = forms.BooleanField(
        label=_("Сбросить пароль отмены"), required=False,
        help_text=_("Пароль отмены задаёт сам сотрудник. После сброса он задаст новый, пока этого не сделано, отмены недоступны."),
    )
    password = forms.CharField(
        label=_("Пароль"), required=False, widget=forms.PasswordInput(render_value=False),
        help_text=_("Для нового сотрудника обязателен. Для существующего — оставьте пустым, чтобы не менять."),
    )
    login_pin = forms.CharField(
        label=_("PIN для терминала"), required=False, max_length=6,
        widget=forms.PasswordInput(render_value=False, attrs={"inputmode": "numeric", "autocomplete": "new-password"}),
        help_text=_("4–6 цифр. С ним официант входит на моноблоке, нажав на своё имя. Пусто — не менять."),
    )
    clear_login_pin = forms.BooleanField(label=_("Убрать PIN для терминала"), required=False)

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "role", "is_active", "hourly_rate", "sales_percent"]
        labels = {
            "username": _("Логин"),
            "first_name": _("Имя"),
            "last_name": _("Фамилия"),
            "role": _("Роль"),
            "is_active": _("Может входить"),
        }
        help_texts = {"username": "", "is_active": ""}
        error_messages = {
            "username": {
                "unique": _("Сотрудник с таким логином уже есть."),
                "invalid": _("Логин может содержать только буквы, цифры и знаки @ . + - _"),
            },
        }

    def __init__(self, *args, editor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.editor = editor
        roles = list(User.Role.choices)
        # Администратор не назначает владельцев и других администраторов (свою роль видит, чтобы её сохранить)
        if editor is not None and not editor.is_superuser and editor.role != User.Role.OWNER:
            own = self.instance.pk is not None and self.instance.pk == editor.pk
            roles = [r for r in roles if r[0] != User.Role.OWNER and (r[0] != User.Role.ADMIN or own)]
        self.fields["role"].choices = roles
        # Ставки необязательны: пусто — значит 0 (сотрудник без оплаты по часам или процента)
        self.fields["hourly_rate"].required = False
        self.fields["sales_percent"].required = False

    def clean_hourly_rate(self):
        value = self.cleaned_data.get("hourly_rate") or 0
        if value < 0:
            raise forms.ValidationError(_("Ставка не может быть меньше нуля."))
        return value

    def clean_sales_percent(self):
        value = self.cleaned_data.get("sales_percent") or 0
        if value < 0 or value > 100:
            raise forms.ValidationError(_("Процент должен быть от 0 до 100."))
        return value

    def clean_password(self):
        password = self.cleaned_data.get("password", "")
        if not self.instance.pk and not password:
            raise forms.ValidationError(_("Задайте пароль."))
        if password:
            try:
                validate_password(password, self.instance)
            except forms.ValidationError as exc:
                raise forms.ValidationError([self._password_message(error) for error in exc.error_list])
        return password

    @staticmethod
    def _password_message(error):
        """Свои тексты вместо встроенных английских: узбекского перевода у Django для них нет."""
        code = error.code
        if code == "password_too_short":
            return _("Пароль слишком короткий: нужно не меньше %(n)s символов.") % {"n": (error.params or {}).get("min_length", 8)}
        if code == "password_too_common":
            return _("Пароль слишком простой. Придумайте другой.")
        if code == "password_entirely_numeric":
            return _("Пароль не должен состоять только из цифр.")
        if code == "password_too_similar":
            return _("Пароль слишком похож на имя или логин.")
        return error.message % (error.params or {}) if error.params else error.message

    def clean_login_pin(self):
        from accounts import terminal

        pin = (self.cleaned_data.get("login_pin") or "").strip()
        if pin:
            reason = terminal.weak_reason(pin)
            if reason:
                raise forms.ValidationError(reason)
        return pin

    def clean(self):
        data = super().clean()
        from accounts.permissions import PIN_LOGIN_ROLES

        if data.get("login_pin") and data.get("role") not in PIN_LOGIN_ROLES:
            self.add_error("login_pin", _("PIN для терминала бывает только у официантов."))
        return data

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

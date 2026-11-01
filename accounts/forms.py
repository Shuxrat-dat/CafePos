from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.forms import AuthenticationForm
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from core.forms import style_form

from . import cancel_pin, login_guard


class GuardedLoginMixin:
    """Не даёт подбирать пароль: после нескольких ошибок вход на время закрыт (см. login_guard)."""

    locked_minutes = 0

    def clean(self):
        username = self.cleaned_data.get("username")
        self.locked_minutes = login_guard.locked_minutes(self.request, username)
        if self.locked_minutes:
            raise forms.ValidationError(
                gettext("Слишком много неверных попыток. Попробуйте через %(m)s мин.") % {"m": self.locked_minutes},
                code="locked",
            )
        try:
            data = super().clean()
        except forms.ValidationError:
            login_guard.register_failure(self.request, username)
            self.locked_minutes = login_guard.locked_minutes(self.request, username)
            raise
        login_guard.reset(self.request, username)
        return data


class LoginForm(GuardedLoginMixin, AuthenticationForm):
    pass


class AdminLoginForm(GuardedLoginMixin, AdminAuthenticationForm):
    pass


class CancelPinForm(forms.Form):
    """Кассир задаёт или меняет свой пароль отмены. Нужен пароль для входа: чтобы за незаблокированной
    кассой чужой человек не поставил свой пароль отмены."""

    account_password = forms.CharField(
        label=_("Ваш пароль для входа"), widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
        help_text=_("Тот, которым вы входите в систему. Нужен, чтобы подтвердить, что это вы."),
    )
    pin = forms.CharField(
        label=_("Новый пароль отмены"), widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text=_("От 4 до 32 символов: цифры или буквы. Не такой же, как пароль для входа. Никому его не говорите."),
    )
    pin2 = forms.CharField(
        label=_("Повторите пароль отмены"), widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        style_form(self)

    def clean_account_password(self):
        value = self.cleaned_data["account_password"]
        if not self.user.check_password(value):
            raise forms.ValidationError(_("Неверный пароль для входа."))
        return value

    def clean_pin(self):
        pin = cancel_pin.normalize(self.cleaned_data["pin"])
        reason = cancel_pin.weak_reason(pin, self.user)
        if reason:
            raise forms.ValidationError(reason)
        return pin

    def clean(self):
        data = super().clean()
        if data.get("pin") and data.get("pin2") is not None and data["pin"] != cancel_pin.normalize(data["pin2"]):
            self.add_error("pin2", _("Пароли не совпадают."))
        return data

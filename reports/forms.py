from django import forms
from django.utils.translation import gettext_lazy as _

from core.forms import StyledModelForm

from .models import Expense


class ExpenseForm(StyledModelForm):
    class Meta:
        model = Expense
        fields = ["date", "category", "amount", "comment"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is None or amount <= 0:
            raise forms.ValidationError(_("Сумма должна быть больше нуля."))
        return amount

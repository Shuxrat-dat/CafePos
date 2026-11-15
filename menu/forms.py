from django import forms
from django.forms import inlineformset_factory
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from core.forms import StyledModelForm, localized_name, style_form
from inventory.models import RecipeItem

from .models import ALLERGEN_CHOICES, WEEKDAY_CHOICES, Category, Dish, Modifier, Promotion

MAX_PHOTO_BYTES = 3 * 1024 * 1024


def _modifier_label(modifier):
    name = localized_name(modifier)
    return f"{gettext(modifier.group)}: {name}" if modifier.group else name


class DishForm(StyledModelForm):
    class Meta:
        model = Dish
        fields = [
            "name", "name_uz", "category", "price", "description", "description_uz",
            "photo", "modifiers", "allergens", "is_available", "is_hidden", "prep_minutes", "sort_order",
            "sold_by_weight",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "description_uz": forms.Textarea(attrs={"rows": 3}),
            "modifiers": forms.CheckboxSelectMultiple,
        }

    allergens = forms.MultipleChoiceField(
        label=_("Аллергены и пометки"), required=False, choices=ALLERGEN_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        help_text=_("Гости увидят эти пометки в меню и смогут выбрать блюдо осознанно."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["prep_minutes"].required = False
        self.fields["category"].empty_label = _("Выберите категорию")
        self.fields["category"].label_from_instance = localized_name
        self.fields["modifiers"].label_from_instance = _modifier_label
        if self.instance and self.instance.pk:
            self.initial["allergens"] = self.instance.allergen_codes

    def clean_allergens(self):
        return ",".join(self.cleaned_data.get("allergens") or [])

    def clean_prep_minutes(self):
        value = self.cleaned_data.get("prep_minutes")
        return min(value or 0, 600)

    def clean_price(self):
        price = self.cleaned_data["price"]
        if price is None or price <= 0:
            raise forms.ValidationError(_("Цена должна быть больше нуля."))
        return price

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if photo and hasattr(photo, "size") and photo.size > MAX_PHOTO_BYTES:
            raise forms.ValidationError(_("Файл больше 3 МБ. Уменьшите фото."))
        return photo


class CategoryForm(StyledModelForm):
    class Meta:
        model = Category
        fields = ["name", "name_uz", "icon", "station", "sort_order", "is_active"]


class PromotionForm(StyledModelForm):
    class Meta:
        model = Promotion
        fields = ["name", "name_uz", "percent", "weekdays", "start_time", "end_time", "date_from", "date_to",
                  "categories", "dishes", "is_active"]
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "end_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "date_from": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "date_to": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "categories": forms.CheckboxSelectMultiple,
            "dishes": forms.CheckboxSelectMultiple,
        }

    weekdays = forms.MultipleChoiceField(
        label=_("Дни недели"), required=False, choices=WEEKDAY_CHOICES, widget=forms.CheckboxSelectMultiple,
        help_text=_("Ничего не отмечено — каждый день."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["categories"].label_from_instance = localized_name
        self.fields["categories"].help_text = _("Не выбрано ни категорий, ни блюд — скидка на всё меню.")
        self.fields["dishes"].queryset = Dish.objects.filter(is_hidden=False).order_by("category__sort_order", "name")
        self.fields["dishes"].label_from_instance = localized_name
        if self.instance and self.instance.pk:
            self.initial["weekdays"] = [d for d in self.instance.weekdays.split(",") if d]
        else:
            self.initial.setdefault("weekdays", [c for c, _label in WEEKDAY_CHOICES])

    def clean_weekdays(self):
        return ",".join(sorted(self.cleaned_data.get("weekdays") or []))

    def clean_percent(self):
        value = self.cleaned_data.get("percent")
        if value is None or not 1 <= value <= 99:
            raise forms.ValidationError(_("Скидка должна быть от 1 до 99%."))
        return value

    def clean(self):
        data = super().clean()
        if bool(data.get("start_time")) != bool(data.get("end_time")):
            self.add_error("end_time", _("Укажите и начало, и конец, или оставьте оба пустыми."))
        if data.get("date_from") and data.get("date_to") and data["date_to"] < data["date_from"]:
            self.add_error("date_to", _("Дата окончания раньше даты начала."))
        return data


class ModifierForm(StyledModelForm):
    class Meta:
        model = Modifier
        fields = ["name", "name_uz", "group", "price_delta", "consumption_multiplier"]


class RecipeItemForm(forms.ModelForm):
    class Meta:
        model = RecipeItem
        fields = ["product", "quantity"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_form(self)
        self.fields["product"].empty_label = _("Выберите продукт")
        self.fields["product"].label_from_instance = lambda product: f"{localized_name(product)} ({product.get_unit_display()})"

    def clean_quantity(self):
        quantity = self.cleaned_data.get("quantity")
        if quantity is not None and quantity <= 0:
            raise forms.ValidationError(_("Расход должен быть больше нуля."))
        return quantity


RecipeFormSet = inlineformset_factory(
    Dish, RecipeItem, form=RecipeItemForm, extra=2, can_delete=True,
)

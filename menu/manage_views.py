"""Управление меню: блюда, категории, модификаторы, техкарты."""
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, ProtectedError, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.permissions import MANAGE_ROLES, role_required

from .forms import CategoryForm, DishForm, ModifierForm, RecipeFormSet
from .models import Category, Dish, Modifier

manager_only = role_required(MANAGE_ROLES)


@manager_only
def manage_menu(request):
    categories = Category.objects.annotate(n=Count("dishes"))
    dishes = Dish.objects.select_related("category").prefetch_related("modifiers", "recipe")
    category_id = request.GET.get("category", "")
    status = request.GET.get("status", "")
    search = request.GET.get("q", "").strip()[:50]
    if category_id.isdigit():
        dishes = dishes.filter(category_id=int(category_id))
    if status == "on":
        dishes = dishes.filter(is_available=True, is_hidden=False)
    elif status == "stop":
        dishes = dishes.filter(is_available=False)
    elif status == "hidden":
        dishes = dishes.filter(is_hidden=True)
    if search:
        dishes = dishes.filter(Q(name__icontains=search) | Q(name_uz__icontains=search))
    context = {
        "categories": categories,
        "dishes": dishes.order_by("category__sort_order", "category__name", "sort_order", "name"),
        "category_id": category_id,
        "status": status,
        "search": search,
        "total": Dish.objects.count(),
        "stop_count": Dish.objects.filter(is_available=False).count(),
        "hidden_count": Dish.objects.filter(is_hidden=True).count(),
    }
    return render(request, "manage/menu.html", context)


def _dish_form(request, dish=None):
    form = DishForm(request.POST or None, request.FILES or None, instance=dish)
    recipe = RecipeFormSet(request.POST or None, instance=dish or Dish(), prefix="recipe")
    if request.method == "POST":
        if form.is_valid() and recipe.is_valid():
            with transaction.atomic():
                saved = form.save()
                recipe.instance = saved
                recipe.save()
            messages.success(request, _("Блюдо «%(name)s» сохранено.") % {"name": saved.name})
            return redirect("manage_menu")
        messages.error(request, _("Проверьте поля формы."))
    return render(request, "manage/dish_form.html", {"form": form, "recipe": recipe, "dish": dish})


@manager_only
def dish_create(request):
    return _dish_form(request)


@manager_only
def dish_edit(request, dish_id):
    return _dish_form(request, get_object_or_404(Dish, id=dish_id))


@manager_only
@require_POST
def dish_toggle(request, dish_id):
    """Быстрое переключение: стоп-лист или скрытие с кассы."""
    dish = get_object_or_404(Dish.objects.select_related("category"), id=dish_id)
    field = request.POST.get("field")
    if field == "available":
        dish.is_available = not dish.is_available
    elif field == "hidden":
        dish.is_hidden = not dish.is_hidden
    dish.save(update_fields=["is_available", "is_hidden"])
    return render(request, "manage/_dish_row.html", {"dish": dish})


@manager_only
@require_POST
def dish_delete(request, dish_id):
    dish = get_object_or_404(Dish, id=dish_id)
    try:
        dish.delete()
    except ProtectedError:
        dish.is_hidden = True
        dish.save(update_fields=["is_hidden"])
        messages.warning(
            request, _("«%(name)s» уже есть в заказах, поэтому блюдо скрыто, а не удалено.") % {"name": dish.name}
        )
    else:
        messages.success(request, _("Блюдо удалено."))
    return redirect("manage_menu")


def _simple_crud(request, model, form_class, template, obj_id=None):
    """Список и форма для категорий и модификаторов на одной странице."""
    instance = get_object_or_404(model, id=obj_id) if obj_id else None
    form = form_class(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Сохранено."))
        return redirect(request.resolver_match.url_name.replace("_edit", ""))
    return form, instance


@manager_only
def manage_categories(request, category_id=None):
    result = _simple_crud(request, Category, CategoryForm, "manage/categories.html", category_id)
    if not isinstance(result, tuple):
        return result
    form, instance = result
    categories = Category.objects.annotate(n=Count("dishes"))
    return render(request, "manage/categories.html", {"form": form, "instance": instance, "categories": categories})


@manager_only
@require_POST
def category_delete(request, category_id):
    category = get_object_or_404(Category, id=category_id)
    try:
        category.delete()
    except ProtectedError:
        messages.error(request, _("В категории есть блюда. Перенесите или удалите их, либо выключите категорию."))
    else:
        messages.success(request, _("Категория удалена."))
    return redirect("manage_categories")


@manager_only
def manage_modifiers(request, modifier_id=None):
    result = _simple_crud(request, Modifier, ModifierForm, "manage/modifiers.html", modifier_id)
    if not isinstance(result, tuple):
        return result
    form, instance = result
    modifiers = Modifier.objects.annotate(n=Count("dish"))
    return render(request, "manage/modifiers.html", {"form": form, "instance": instance, "modifiers": modifiers})


@manager_only
@require_POST
def modifier_delete(request, modifier_id):
    from pos.models import OrderItem

    modifier = get_object_or_404(Modifier, id=modifier_id)
    if OrderItem.objects.filter(modifiers=modifier).exists():
        # Удаление изменило бы суммы старых заказов
        messages.error(request, _("Модификатор уже есть в заказах. Уберите его из блюд вместо удаления."))
    else:
        modifier.delete()
        messages.success(request, _("Модификатор удалён."))
    return redirect("manage_modifiers")

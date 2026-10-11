from django.contrib import admin

from inventory.models import RecipeItem

from .models import Category, Dish, Modifier


class RecipeInline(admin.TabularInline):
    model = RecipeItem
    extra = 1


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")


@admin.register(Modifier)
class ModifierAdmin(admin.ModelAdmin):
    list_display = ("name", "price_delta")


@admin.register(Dish)
class DishAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "price", "is_available", "cost", "margin")
    list_filter = ("category", "is_available")
    list_editable = ("price", "is_available")
    search_fields = ("name",)
    filter_horizontal = ("modifiers",)
    inlines = [RecipeInline]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("recipe__product")

    def _cost(self, obj):
        recipe = list(obj.recipe.all())
        if not recipe:
            return None
        return sum(r.quantity * r.product.cost_per_unit for r in recipe)

    @admin.display(description="Себестоимость")
    def cost(self, obj):
        value = self._cost(obj)
        return "—" if value is None else round(value)

    @admin.display(description="Маржа")
    def margin(self, obj):
        value = self._cost(obj)
        return "—" if value is None else round(obj.price - value)
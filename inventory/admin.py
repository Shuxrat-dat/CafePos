from django.contrib import admin
from django.utils.html import format_html

from .models import Product, StockMovement


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "stock_display", "min_stock", "state", "cost_per_unit")
    search_fields = ("name",)

    @admin.display(description="Остаток")
    def stock_display(self, obj):
        return f"{obj.stock:f} {obj.get_unit_display()}"

    @admin.display(description="Состояние")
    def state(self, obj):
        if obj.stock <= obj.min_stock:
            return format_html('<b style="color:{}">{}</b>', "#c00", "Мало")
        return "ОК"


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ("created_at", "product", "kind", "quantity", "unit_cost", "order", "created_by")
    list_filter = ("kind", "product")
    fields = ("product", "kind", "quantity", "comment")
    date_hierarchy = "created_at"

    def has_change_permission(self, request, obj=None):
        # Созданное движение менять нельзя, иначе остаток разойдётся с историей
        return obj is None

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
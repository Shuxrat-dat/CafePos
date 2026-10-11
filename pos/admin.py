from django.contrib import admin

from .models import Hall, Order, OrderItem, Table


class TableInline(admin.TabularInline):
    model = Table
    extra = 1


@admin.register(Hall)
class HallAdmin(admin.ModelAdmin):
    inlines = [TableInline]


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 1


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "table", "waiter", "status", "created_at", "order_total")
    list_filter = ("status", "table__hall")
    inlines = [OrderItemInline]

    @admin.display(description="Сумма (сум)")
    def order_total(self, obj):
        return obj.total

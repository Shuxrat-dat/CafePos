from django.contrib import admin

from .models import CashMovement, CashShift, Hall, Order, OrderItem, Payment, Table


class TableInline(admin.TabularInline):
    model = Table
    extra = 1


@admin.register(Hall)
class HallAdmin(admin.ModelAdmin):
    inlines = [TableInline]


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 1


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "table", "waiter", "cashier", "shift", "status", "created_at", "order_total")
    list_filter = ("status", "table__hall", "shift")
    inlines = [OrderItemInline, PaymentInline]

    @admin.display(description="Сумма (сум)")
    def order_total(self, obj):
        return obj.total


class CashMovementInline(admin.TabularInline):
    model = CashMovement
    extra = 0


@admin.register(CashShift)
class CashShiftAdmin(admin.ModelAdmin):
    list_display = ("id", "cashier", "opened_at", "closed_at", "status", "initial_cash", "expected_cash", "actual_cash", "difference")
    list_filter = ("status", "cashier")
    inlines = [CashMovementInline]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "shift", "payment_method", "amount", "cashier", "is_refund", "created_at")
    list_filter = ("payment_method", "is_refund", "shift")


@admin.register(CashMovement)
class CashMovementAdmin(admin.ModelAdmin):
    list_display = ("id", "shift", "kind", "amount", "reason", "created_by", "created_at")
    list_filter = ("kind", "shift")


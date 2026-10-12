from django.contrib import admin

from .models import CafeSettings


@admin.register(CafeSettings)
class CafeSettingsAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "service_percent", "default_language")

    def has_add_permission(self, request):
        # Настройки заведения — одна запись
        return not CafeSettings.objects.exists()

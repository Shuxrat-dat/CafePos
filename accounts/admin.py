from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .forms import AdminLoginForm
from .models import User

admin.site.login_form = AdminLoginForm


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("Роль", {"fields": ("role",)}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("Роль", {"fields": ("role",)}),)
    list_display = ("username", "first_name", "role", "is_active")
    list_filter = ("role", "is_active")
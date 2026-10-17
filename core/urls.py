from django.urls import include, path

from . import views

urlpatterns = [
    path("settings/", views.manage_settings, name="manage_settings"),
    path("telegram/", views.manage_telegram, name="manage_telegram"),
    path("audit/", views.manage_audit, name="manage_audit"),
    path("tables/", views.manage_tables, name="manage_tables"),
    path("tables/<int:table_id>/", views.table_edit, name="table_edit"),
    path("tables/<int:table_id>/delete/", views.table_delete, name="table_delete"),
    path("halls/<int:hall_id>/delete/", views.hall_delete, name="hall_delete"),
    path("staff/", views.manage_staff, name="manage_staff"),
    path("staff/new/", views.staff_form, name="staff_create"),
    path("staff/<int:user_id>/", views.staff_form, name="staff_edit"),
    path("demo/reset/", views.demo_reset, name="demo_reset"),
    path("menu/", include("menu.manage_urls")),
]

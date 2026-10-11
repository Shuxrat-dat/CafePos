from django.urls import path

from pos import guest_views

from . import public_views

urlpatterns = [
    path("", public_views.public_menu, name="public_menu"),
    path("qr/", public_views.menu_qr, name="menu_qr"),
    path("qr/tables/", guest_views.tables_qr, name="tables_qr"),
    path("t/<int:table_id>/", guest_views.table_menu, name="table_menu"),
]
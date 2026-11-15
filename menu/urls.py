from django.urls import path

from pos import guest_views

from . import booking_views, public_views

urlpatterns = [
    path("", public_views.public_menu, name="public_menu"),
    path("book/", booking_views.book, name="book"),
    path("book/done/", booking_views.book_done, name="book_done"),
    path("qr/", public_views.menu_qr, name="menu_qr"),
    path("qr/tables/", guest_views.tables_qr, name="tables_qr"),
    path("t/<int:table_id>/", guest_views.table_menu, name="table_menu"),
    path("review/<str:token>/", guest_views.review, name="order_review"),
]

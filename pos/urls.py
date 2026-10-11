from django.urls import path

from . import guest_views, views

urlpatterns = [
    path("", views.floor, name="floor"),
    path("tables/<int:table_id>/open/", views.open_table, name="open_table"),
    path("orders/<int:order_id>/", views.order_detail, name="order_detail"),
    path("orders/<int:order_id>/panel/", views.order_panel, name="order_panel"),
    path("orders/<int:order_id>/dish/<int:dish_id>/form/", views.item_form, name="item_form"),
    path("orders/<int:order_id>/add/<int:dish_id>/", views.add_item, name="add_item"),
    path("orders/<int:order_id>/item/<int:item_id>/edit/", views.edit_item, name="edit_item"),
    path("orders/<int:order_id>/item/<int:item_id>/qty/", views.change_qty, name="change_qty"),
    path("orders/<int:order_id>/send/", views.send_to_kitchen, name="send_to_kitchen"),
    path("orders/<int:order_id>/item/<int:item_id>/serve/", views.serve_item, name="serve_item"),
    path("orders/<int:order_id>/cancel/", views.cancel_order, name="cancel_order"),
    path("orders/<int:order_id>/move/", views.move_form, name="move_form"),
    path("orders/<int:order_id>/move/<int:table_id>/", views.move_order, name="move_order"),
    path("orders/<int:order_id>/pay/", views.pay, name="pay"),
    path("orders/<int:order_id>/receipt/", views.receipt, name="receipt"),
    path("orders/<int:order_id>/guest/<int:guest_id>/accept/", guest_views.accept_guest, name="accept_guest"),
    path("orders/<int:order_id>/guest/<int:guest_id>/reject/", guest_views.reject_guest, name="reject_guest"),
]
from django.urls import path

from . import views

urlpatterns = [
    path("", views.stock, name="stock"),
    path("shopping/", views.shopping, name="stock_shopping"),
    path("count/", views.stock_count, name="stock_count"),
    path("writeoff/", views.stock_writeoff, name="stock_writeoff"),
    path("movements/", views.stock_movements, name="stock_movements"),
    path("product/new/", views.product_form, name="product_create"),
    path("product/<int:product_id>/", views.product_form, name="product_edit"),
]

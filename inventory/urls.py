from django.urls import path

from . import views

urlpatterns = [
    path("", views.stock, name="stock"),
    path("count/", views.stock_count, name="stock_count"),
]
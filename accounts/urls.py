from django.urls import path

from . import views

urlpatterns = [
    path("cancel-pin/", views.cancel_pin_settings, name="cancel_pin_settings"),
]

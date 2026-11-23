from django.urls import path

from . import views

urlpatterns = [
    path("cancel-pin/", views.cancel_pin_settings, name="cancel_pin_settings"),
    path("clock/", views.clock, name="clock"),
    path("earnings/", views.my_earnings, name="my_earnings"),
]

terminal_patterns = [
    path("", views.terminal_home, name="terminal_home"),
    path("login/", views.terminal_login, name="terminal_login"),
    path("lock/", views.terminal_lock, name="terminal_lock"),
]

manage_patterns = [
    path("", views.manage_terminals, name="manage_terminals"),
]

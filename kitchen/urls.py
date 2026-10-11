from django.urls import path

from . import views

urlpatterns = [
    path("", views.board, name="kitchen_board"),
    path("tickets/", views.tickets, name="kitchen_tickets"),
    path("item/<int:item_id>/ready/", views.mark_ready, name="mark_ready"),
]
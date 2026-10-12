from django.urls import path

from . import views

urlpatterns = [
    path("", views.board, name="kitchen_board"),
    path("tickets/", views.tickets, name="kitchen_tickets"),
    path("item/<int:item_id>/ready/", views.mark_ready, name="mark_ready"),
    path("order/<int:order_id>/start/", views.ticket_start, name="ticket_start"),
    path("order/<int:order_id>/ready/", views.ticket_ready, name="ticket_ready"),
    path("order/<int:order_id>/served/", views.ticket_served, name="ticket_served"),
]

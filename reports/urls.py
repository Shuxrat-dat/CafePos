from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="reports"),
    path("expenses/", views.expenses, name="expenses"),
    path("expenses/<int:expense_id>/delete/", views.expense_delete, name="expense_delete"),
]

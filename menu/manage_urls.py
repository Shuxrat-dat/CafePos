from django.urls import path

from . import manage_views as views

urlpatterns = [
    path("", views.manage_menu, name="manage_menu"),
    path("dish/new/", views.dish_create, name="dish_create"),
    path("dish/<int:dish_id>/", views.dish_edit, name="dish_edit"),
    path("dish/<int:dish_id>/toggle/", views.dish_toggle, name="dish_toggle"),
    path("dish/<int:dish_id>/delete/", views.dish_delete, name="dish_delete"),
    path("categories/", views.manage_categories, name="manage_categories"),
    path("categories/<int:category_id>/", views.manage_categories, name="manage_categories_edit"),
    path("categories/<int:category_id>/delete/", views.category_delete, name="category_delete"),
    path("modifiers/", views.manage_modifiers, name="manage_modifiers"),
    path("modifiers/<int:modifier_id>/", views.manage_modifiers, name="manage_modifiers_edit"),
    path("modifiers/<int:modifier_id>/delete/", views.modifier_delete, name="modifier_delete"),
]

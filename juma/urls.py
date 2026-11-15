from django.urls import path

from . import views

urlpatterns = [
    path("", views.board, name="juma_board"),
    path("panel/", views.panel, name="juma_panel"),
    path("pause/", views.pause, name="juma_pause"),
    path("<int:link_id>/accept/", views.accept, name="juma_accept"),
    path("<int:link_id>/paid/", views.paid, name="juma_paid"),
    path("<int:link_id>/ready/", views.ready, name="juma_ready"),
    path("<int:link_id>/handover/", views.handover, name="juma_handover"),
    path("<int:link_id>/cancel/", views.cancel, name="juma_cancel"),
    path("<int:link_id>/refund/", views.refund_done, name="juma_refund"),
]

from django.urls import path

from . import views

manage_patterns = [
    path("", views.manage_printers, name="manage_printers"),
    path("new/", views.printer_form, name="printer_create"),
    path("<int:printer_id>/", views.printer_form, name="printer_edit"),
    path("<int:printer_id>/test/", views.printer_test, name="printer_test"),
    path("<int:printer_id>/delete/", views.printer_delete, name="printer_delete"),
    path("agent.zip", views.agent_download, name="print_agent_download"),
]

order_patterns = [
    path("<int:order_id>/", views.print_order, name="print_order"),
    path("<int:order_id>/status/", views.order_print_status, name="order_print_status"),
]

agent_patterns = [
    path("jobs/", views.agent_jobs, name="print_agent_jobs"),
    path("jobs/<int:job_id>/result/", views.agent_result, name="print_agent_result"),
    path("status/", views.agent_status, name="print_agent_status"),
]

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path
from django.views.static import serve

from core import views as core_views
from printing import urls as printing_urls

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("lang/", core_views.set_language, name="set_language"),
    path("ping/", core_views.ping, name="ping"),
    path("demo/login/", core_views.demo_login, name="demo_login"),
    path("kitchen/", include("kitchen.urls")),
    path("reports/", include("reports.urls")),
    path("stock/", include("inventory.urls")),
    path("menu/", include("menu.urls")),
    path("manage/printers/", include(printing_urls.manage_patterns)),
    path("manage/", include("core.urls")),
    path("print/", include(printing_urls.order_patterns)),
    # Программа печати в кафе: вход по токену, без сессии и CSRF
    path("print-agent/", include(printing_urls.agent_patterns)),
    # Фото блюд и логотип. Касса работает в локальной сети без отдельного веб-сервера.
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
    path("", include("pos.urls")),
]

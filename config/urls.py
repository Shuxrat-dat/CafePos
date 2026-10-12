from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path
from django.views.static import serve

from core import views as core_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("lang/", core_views.set_language, name="set_language"),
    path("demo/login/", core_views.demo_login, name="demo_login"),
    path("kitchen/", include("kitchen.urls")),
    path("reports/", include("reports.urls")),
    path("stock/", include("inventory.urls")),
    path("menu/", include("menu.urls")),
    path("manage/", include("core.urls")),
    # Фото блюд и логотип. Касса работает в локальной сети без отдельного веб-сервера.
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
    path("", include("pos.urls")),
]

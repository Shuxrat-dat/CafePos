from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    verbose_name = "Заведение"

    def ready(self):
        from django.contrib.auth.signals import user_login_failed

        from . import audit

        def on_failed_login(sender, credentials, request=None, **kwargs):
            """Записываем неудачные входы: это ранний признак подбора пароля."""
            ip = request.META.get("REMOTE_ADDR", "") if request is not None else ""
            username = str((credentials or {}).get("username", ""))[:60]
            audit.record(None, audit.A.LOGIN_FAILED, detail=f"{username} · {ip}")

        user_login_failed.connect(on_failed_login, dispatch_uid="cafepos_audit_failed_login")

from django.conf import settings
from django.utils import translation

from .models import CafeSettings

SUPPORTED = {code for code, _ in settings.LANGUAGES}


class LanguageMiddleware:
    """Язык интерфейса: выбор на этом устройстве (cookie), иначе язык заведения из настроек.
    Язык браузера не учитываем: на кассе язык выбирает сотрудник, а не система."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.cafe = CafeSettings.get()
        language = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
        if language not in SUPPORTED:
            language = request.cafe.default_language
        if language not in SUPPORTED:
            language = settings.LANGUAGE_CODE
        translation.activate(language)
        request.LANGUAGE_CODE = language
        response = self.get_response(request)
        response.headers.setdefault("Content-Language", language)
        translation.deactivate()
        return response

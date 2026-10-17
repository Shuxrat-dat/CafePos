"""Оценка гостя по QR на чеке. Ссылка подписана: номер заказа в ней нельзя подобрать или подменить."""
from django.core import signing

SALT = "cafepos.review"
MAX_AGE_SECONDS = 14 * 24 * 3600  # две недели на то, чтобы оценить визит


def make_token(order):
    return signing.dumps(order.pk, salt=SALT)


def read_token(token):
    """Номер заказа из ссылки или None, если ссылка повреждена или просрочена."""
    try:
        return signing.loads(token, salt=SALT, max_age=MAX_AGE_SECONDS)
    except signing.BadSignature:
        return None

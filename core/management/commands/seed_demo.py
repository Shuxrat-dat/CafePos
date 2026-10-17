"""Демо-ресторан «Cafe Demo» для презентаций.

Работает только в демо-режиме (CAFEPOS_DEMO=1, отдельная база demo.sqlite3).
Все суммы, оплаты, остатки и отчёты получаются через те же функции, что и в рабочей кассе:
заказы оплачиваются через pos.services, продукты списываются по техкартам, отчёты считают из этих записей.
Случайность фиксирована (seed), поэтому после каждого сброса данные одинаковые.
"""
import random
from datetime import datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import CafeSettings
from inventory.models import Product, RecipeItem, StockMovement
from inventory.services import write_off_order
from menu.models import Category, Dish, Modifier
from pos.models import GuestOrder, GuestOrderItem, Hall, Order, OrderItem, Table, round_sum

User = get_user_model()
PASSWORD = "demo1234"  # только для демо-базы; записан в docs/DEMO.md

USERS = [
    ("owner", "Aziz", "Karimov", "owner"),
    ("admin", "Dilnoza", "Rahimova", "admin"),
    ("kassir", "Malika", "Yusupova", "cashier"),
    ("ofitsiant", "Jasur", "Toshmatov", "waiter"),
    ("ofitsiant2", "Sardor", "Aliyev", "waiter"),
    ("oshpaz", "Rustam", "Nazarov", "cook"),
]

# (ключ, название, по-узбекски, единица, цена за единицу, минимальный остаток)
PRODUCTS = [
    ("rice", "Рис девзира", "Devzira guruch", "g", 30, 5000),
    ("lamb", "Баранина", "Qoʻy goʻshti", "g", 145, 4000),
    ("beef", "Говядина", "Mol goʻshti", "g", 125, 4000),
    ("chicken", "Куриное филе", "Tovuq filesi", "g", 55, 3000),
    ("carrot", "Морковь", "Sabzi", "g", 7, 5000),
    ("onion", "Лук репчатый", "Piyoz", "g", 5, 4000),
    ("potato", "Картофель", "Kartoshka", "g", 6, 5000),
    ("tomato", "Помидоры", "Pomidor", "g", 18, 3000),
    ("cucumber", "Огурцы", "Bodring", "g", 15, 2000),
    ("flour", "Мука", "Un", "g", 7, 5000),
    ("oil", "Масло растительное", "Oʻsimlik yogʻi", "ml", 22, 3000),
    ("cheese", "Сыр твёрдый", "Qattiq pishloq", "g", 95, 2000),
    ("bun", "Булочка для бургера", "Burger noni", "pcs", 2500, 30),
    ("coffee", "Кофе в зёрнах", "Qahva donlari", "g", 320, 1500),
    ("milk", "Молоко", "Sut", "ml", 13, 5000),
    ("green_tea", "Чай зелёный", "Koʻk choy", "g", 150, 500),
    ("black_tea", "Чай чёрный", "Qora choy", "g", 140, 500),
    ("sugar", "Сахар", "Shakar", "g", 12, 3000),
    ("lemon", "Лимон", "Limon", "pcs", 2500, 20),
    ("cola", "Coca-Cola 0,5 л", "Coca-Cola 0,5 l", "pcs", 6500, 24),
    ("water", "Вода 0,5 л", "Suv 0,5 l", "pcs", 2500, 24),
    ("orange", "Апельсины", "Apelsin", "g", 22, 3000),
    ("cream_cheese", "Сливочный сыр", "Qaymoqli pishloq", "g", 110, 1000),
    ("cream", "Сливки", "Qaymoq", "ml", 45, 1000),
    ("noodles", "Лапша для лагмана", "Lagʻmon ugrasi", "g", 25, 2000),
    ("ice_cream", "Мороженое пломбир", "Plombir muzqaymoq", "g", 60, 1500),
    ("lettuce", "Салат листовой", "Salat bargi", "g", 40, 500),
    ("dough", "Тесто для пиццы", "Pitstsa xamiri", "pcs", 6000, 10),
    ("mozzarella", "Моцарелла", "Motsarella", "g", 110, 2000),
    ("pepperoni", "Колбаса пепперони", "Pepperoni kolbasa", "g", 140, 800),
    ("sausage", "Сосиски", "Sosiska", "pcs", 3500, 20),
]
# Эти продукты в демо специально заканчиваются: видно уведомление о низком остатке
LOW_STOCK = {"bun", "coffee", "lemon", "mozzarella"}

CATEGORIES = [
    ("national", "Национальная кухня", "Milliy taomlar", "bowl"),
    ("hot", "Горячие блюда", "Issiq taomlar", "plate"),
    ("fastfood", "Фастфуд", "Fastfud", "burger"),
    ("pizza", "Пицца", "Pitstsa", "pizza"),
    ("salads", "Салаты", "Salatlar", "salad"),
    ("desserts", "Десерты", "Desertlar", "cake"),
    ("coffee", "Кофе", "Qahva", "coffee"),
    ("drinks", "Напитки", "Ichimliklar", "drink"),
]

# Пометки об аллергенах по ключевым словам в названии блюда (для демо-меню)
ALLERGEN_KEYWORDS = {
    "gluten": ["лагман", "манты", "самса", "лепёш", "лепеш", "пицца", "бургер", "сэндвич", "круассан", "чизкейк", "торт", "хот-дог", "паста", "блин"],
    "milk": ["сыр", "чизкейк", "латте", "капучино", "торт", "мороженое", "пицца", "сливк", "флэт", "какао"],
    "eggs": ["цезарь", "торт", "чизкейк", "омлет", "блин"],
    "nuts": ["орех", "медовик", "ореш"],
    "fish": ["рыб", "лосос", "форел"],
    "seafood": ["креветк", "мидии"],
    "soy": ["соев"],
    "spicy": ["пепперони", "чили", "острый", "острая", "кимчи"],
    "vegan": ["овощн", "греческий"],
}


def allergens_for(name):
    low = name.lower()
    return ",".join(code for code, words in ALLERGEN_KEYWORDS.items() if any(w in low for w in words))


# Время приготовления по категориям, мин: кухня подсвечивает блюда, которые готовятся дольше
PREP_MINUTES = {"national": 18, "hot": 15, "fastfood": 8, "pizza": 14, "salads": 5, "desserts": 4, "coffee": 3, "drinks": 2}

# (ключ, название, по-узбекски, группа, доплата, множитель расхода)
MODIFIERS = [
    ("double", "Двойная порция", "Ikki hissa", "", 35000, 2),
    ("cup_s", "Маленький 250 мл", "Kichik 250 ml", "Размер", 0, 1),
    ("cup_m", "Средний 350 мл", "Oʻrta 350 ml", "Размер", 5000, Decimal("1.3")),
    ("cup_l", "Большой 450 мл", "Katta 450 ml", "Размер", 9000, Decimal("1.6")),
    ("syrup", "Сироп (ваниль, карамель)", "Sirop (vanil, karamel)", "", 5000, 1),
    ("pizza_30", "30 см", "30 sm", "Размер", 0, 1),
    ("pizza_40", "40 см", "40 sm", "Размер", 30000, Decimal("1.7")),
    ("lemon_classic", "Классический", "Klassik", "Вкус", 0, 1),
    ("lemon_straw", "Клубника", "Qulupnay", "Вкус", 3000, 1),
    ("lemon_passion", "Маракуйя", "Marakuyya", "Вкус", 5000, 1),
    ("extra_cheese", "Доп. сыр", "Qoʻshimcha pishloq", "", 6000, 1),
    ("double_patty", "Двойная котлета", "Ikki kotlet", "", 18000, Decimal("1.8")),
]

# (ключ, категория, название, по-узбекски, цена, вес популярности, техкарта, модификаторы, описание ru, описание uz)
DISHES = [
    ("plov", "national", "Плов по-ташкентски", "Toshkent oshi", 45000, 10,
     {"rice": 180, "lamb": 120, "carrot": 120, "onion": 30, "oil": 40}, ["double"],
     "Рис девзира, баранина, жёлтая морковь, нут", "Devzira guruch, qoʻy goʻshti, sariq sabzi, no'xat"),
    ("lagman", "national", "Лагман", "Lagʻmon", 38000, 7,
     {"noodles": 200, "beef": 100, "carrot": 30, "onion": 30, "tomato": 50, "oil": 20}, [],
     "Тянутая лапша с говядиной и овощами", "Mol goʻshti va sabzavotli choʻzma lagʻmon"),
    ("shurpa", "national", "Шурпа", "Shoʻrva", 36000, 4,
     {"lamb": 150, "potato": 120, "carrot": 60, "onion": 30, "tomato": 40}, [],
     "Наваристый суп из баранины", "Qoʻy goʻshtidan quyuq shoʻrva"),
    ("manti", "national", "Манты, 5 шт", "Manti, 5 dona", 35000, 5,
     {"flour": 150, "lamb": 130, "onion": 80, "oil": 10}, [],
     "С бараниной и луком, подаются со сметаной", "Qoʻy goʻshti va piyozli, qaymoq bilan"),
    ("samsa", "national", "Самса с мясом", "Goʻshtli somsa", 12000, 9,
     {"flour": 90, "lamb": 60, "onion": 40, "oil": 10}, [],
     "Из тандыра", "Tandir somsa"),
    ("kebab_lamb", "national", "Шашлык из баранины", "Qoʻy goʻshtidan kabob", 28000, 8,
     {"lamb": 150, "onion": 30}, [], "1 шампур, с луком и зеленью", "1 six, piyoz va koʻkat bilan"),
    ("kebab_beef", "national", "Шашлык из говядины", "Mol goʻshtidan kabob", 25000, 6,
     {"beef": 150, "onion": 30}, [], "1 шампур", "1 six"),
    ("naryn", "national", "Нарын", "Norin", 40000, 0,
     {"flour": 150, "beef": 120}, [], "Холодная лапша с кониной", "Sovuq norin"),
    ("steak", "hot", "Стейк из говядины", "Mol goʻshti steyki", 95000, 2,
     {"beef": 300, "oil": 20, "potato": 150}, [], "300 г, с картофелем", "300 g, kartoshka bilan"),
    ("chicken_grill", "hot", "Куриное филе гриль", "Gril tovuq filesi", 52000, 3,
     {"chicken": 250, "oil": 15, "cucumber": 50, "tomato": 60}, [], "С овощами", "Sabzavotlar bilan"),
    ("qovurdoq", "hot", "Жаркое по-домашнему", "Uy qovurdogʻi", 48000, 3,
     {"beef": 200, "potato": 200, "onion": 40, "oil": 30}, [], "Говядина с картофелем", "Mol goʻshti va kartoshka"),
    ("burger", "fastfood", "Бургер классический", "Klassik burger", 42000, 5,
     {"bun": 1, "beef": 150, "tomato": 30, "lettuce": 15, "onion": 15}, ["extra_cheese", "double_patty"],
     "Говяжья котлета, овощи, соус", "Mol goʻshti kotleti, sabzavot, sous"),
    ("cheeseburger", "fastfood", "Чизбургер", "Chizburger", 46000, 4,
     {"bun": 1, "beef": 150, "cheese": 40, "tomato": 30, "lettuce": 15}, ["extra_cheese", "double_patty"],
     "С сыром чеддер", "Chedder pishlogʻi bilan"),
    ("fries", "fastfood", "Картофель фри", "Kartoshka fri", 18000, 6,
     {"potato": 200, "oil": 30}, [], "", ""),
    ("hotdog", "fastfood", "Хот-дог", "Xot-dog", 25000, 3,
     {"sausage": 1, "bun": 1, "onion": 10}, [], "", ""),
    ("nuggets", "fastfood", "Наггетсы, 8 шт", "Naggets, 8 dona", 28000, 3,
     {"chicken": 180, "flour": 30, "oil": 40}, [], "", ""),
    ("margherita", "pizza", "Пицца Маргарита", "Margarita pitstsasi", 65000, 3,
     {"dough": 1, "mozzarella": 180, "tomato": 100}, ["pizza_30", "pizza_40"],
     "Томаты, моцарелла, базилик", "Pomidor, motsarella, rayhon"),
    ("pepperoni_pizza", "pizza", "Пицца Пепперони", "Pepperoni pitstsasi", 75000, 3,
     {"dough": 1, "mozzarella": 160, "pepperoni": 80, "tomato": 80}, ["pizza_30", "pizza_40"],
     "Острая колбаса пепперони", "Achchiq pepperoni kolbasasi"),
    ("achichuk", "salads", "Ачичук", "Achchiq-chuchuk", 15000, 6,
     {"tomato": 150, "cucumber": 50, "onion": 30}, [], "Томаты, лук, острый перец", "Pomidor, piyoz, achchiq qalampir"),
    ("caesar", "salads", "Цезарь с курицей", "Tovuqli Sezar", 42000, 4,
     {"chicken": 120, "lettuce": 80, "cheese": 25, "tomato": 40}, [], "", ""),
    ("greek", "salads", "Греческий салат", "Grek salati", 38000, 3,
     {"tomato": 100, "cucumber": 80, "cheese": 60, "oil": 15}, [], "", ""),
    ("season_salad", "salads", "Сезонный салат", "Mavsumiy salat", 30000, 0,
     {"tomato": 100, "cucumber": 100}, [], "Готовится только летом", "Faqat yozda"),
    ("cheesecake", "desserts", "Чизкейк", "Chizkeyk", 32000, 3,
     {"cream_cheese": 120, "sugar": 30, "cream": 30, "flour": 20}, [], "", ""),
    ("medovik", "desserts", "Медовик", "Asalli tort", 28000, 3,
     {"flour": 60, "sugar": 30, "cream": 40}, [], "", ""),
    ("ice_cream", "desserts", "Мороженое, 3 шарика", "Muzqaymoq, 3 shar", 18000, 2,
     {"ice_cream": 150}, [], "", ""),
    ("espresso", "coffee", "Эспрессо", "Espresso", 18000, 2, {"coffee": 9}, [], "", ""),
    ("americano", "coffee", "Американо", "Amerikano", 20000, 4, {"coffee": 9}, ["cup_s", "cup_m", "cup_l"], "", ""),
    ("cappuccino", "coffee", "Капучино", "Kapuchino", 26000, 5,
     {"coffee": 9, "milk": 150}, ["cup_s", "cup_m", "cup_l", "syrup"], "", ""),
    ("latte", "coffee", "Латте", "Latte", 28000, 4,
     {"coffee": 9, "milk": 220}, ["cup_s", "cup_m", "cup_l", "syrup"], "", ""),
    ("raf", "coffee", "Раф", "Raf", 32000, 2, {"coffee": 9, "cream": 120, "sugar": 10}, ["syrup"], "", ""),
    ("green_tea", "drinks", "Чай зелёный, чайник", "Koʻk choy, choynak", 10000, 9, {"green_tea": 8}, [], "", ""),
    ("black_tea", "drinks", "Чай чёрный с лимоном", "Limonli qora choy", 12000, 6,
     {"black_tea": 8, "lemon": Decimal("0.25")}, [], "", ""),
    ("lemonade", "drinks", "Лимонад домашний", "Uy limonadi", 25000, 4,
     {"lemon": 1, "sugar": 40}, ["lemon_classic", "lemon_straw", "lemon_passion"], "1 л", "1 l"),
    ("orange_juice", "drinks", "Апельсиновый фреш", "Apelsin fresh", 35000, 2, {"orange": 400}, [], "", ""),
    ("cola", "drinks", "Coca-Cola 0,5 л", "Coca-Cola 0,5 l", 12000, 5, {"cola": 1}, [], "", ""),
    ("water", "drinks", "Вода 0,5 л", "Suv 0,5 l", 6000, 3, {"water": 1}, [], "", ""),
]
STOP_LIST = {"naryn"}
HIDDEN = {"season_salad"}

HALLS = [
    ("Основной зал", "Asosiy zal", [("1", 4), ("2", 4), ("3", 2), ("4", 4), ("5", 6), ("6", 6), ("7", 4), ("8", 2)]),
    ("Терраса", "Ayvon", [("T1", 4), ("T2", 4), ("T3", 6), ("T4", 8)]),
    ("VIP", "VIP", [("V1", 10), ("V2", 12)]),
]

CANCEL_REASONS = ["Mehmon kutmasdan ketdi", "Buyurtma xato kiritildi", "Mehmon fikridan qaytdi"]


class Command(BaseCommand):
    help = "Заполняет демо-базу рестораном «Cafe Demo» (только при CAFEPOS_DEMO=1)."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Очистить демо-базу и заполнить заново")
        parser.add_argument("--if-empty", action="store_true", help="Заполнить, только если база пустая")
        parser.add_argument("--days", type=int, default=14, help="Сколько дней истории продаж создать")

    def handle(self, *args, **options):
        db_name = Path(str(settings.DATABASES["default"]["NAME"])).name
        if not settings.DEMO_MODE or db_name != "demo.sqlite3":
            raise CommandError(
                "Демо-данные создаются только в демо-режиме (CAFEPOS_DEMO=1, база demo.sqlite3). "
                "Рабочая база не изменена."
            )
        has_data = User.objects.exists() or Order.objects.exists()
        if has_data and options["if_empty"]:
            self.stdout.write("Демо-база уже заполнена, пропускаю.")
            return
        if has_data and not options["reset"]:
            raise CommandError("В демо-базе уже есть данные. Для пересоздания запустите с --reset.")
        if options["reset"]:
            call_command("flush", interactive=False, verbosity=0)

        self.rng = random.Random(2026)
        self.now = timezone.now()
        self.today = timezone.localdate()
        with transaction.atomic():
            self._settings()
            self._users()
            self._floor()
            self._menu()
            self._stock_items()
            self._history(options["days"])
            self._active_orders()
            self._stock_income(options["days"])
            self._audit_log(options["days"])
            self._guest_data()
        self.stdout.write(self.style.SUCCESS(
            f"Демо-ресторан готов: заказов {Order.objects.count()}, блюд {Dish.objects.count()}, "
            f"продуктов {Product.objects.count()}. Пароль сотрудников: {PASSWORD}"
        ))

    # ---------- справочники ----------

    def _settings(self):
        cafe = CafeSettings.get()
        cafe.name = "Cafe Demo"
        cafe.address = "Toshkent sh., Amir Temur koʻchasi, 15"
        cafe.phone = "+998 71 200-00-00"
        cafe.default_language = "uz"
        cafe.service_percent = Decimal(10)
        cafe.vat_percent = Decimal(12)
        cafe.receipt_header = "Wi-Fi: CafeDemo / 12345678"
        cafe.receipt_footer = "Rahmat! Yana kutamiz · Спасибо за визит!"
        cafe.kitchen_warn_minutes = 15
        cafe.save()
        self.cafe = cafe

    def _users(self):
        self.users = {}
        for username, first, last, role in USERS:
            user = User(username=username, first_name=first, last_name=last, role=role)
            user.set_password(PASSWORD)
            user.save()
            self.users[username] = user
        self.waiters = [self.users["ofitsiant"], self.users["ofitsiant2"]]

    def _floor(self):
        self.tables = {}
        for order, (name, name_uz, tables) in enumerate(HALLS):
            hall = Hall.objects.create(name=name, name_uz=name_uz, sort_order=order)
            for number, seats in tables:
                self.tables[number] = Table.objects.create(hall=hall, number=number, seats=seats)

    def _menu(self):
        self.categories = {}
        for order, (key, name, name_uz, icon) in enumerate(CATEGORIES):
            self.categories[key] = Category.objects.create(name=name, name_uz=name_uz, icon=icon, sort_order=order)
        self.modifiers = {}
        for key, name, name_uz, group, delta, mult in MODIFIERS:
            self.modifiers[key] = Modifier.objects.create(
                name=name, name_uz=name_uz, group=group, price_delta=delta, consumption_multiplier=mult
            )
        self.dishes, self.weights = {}, {}
        for order, (key, cat, name, name_uz, price, weight, _recipe, mods, desc, desc_uz) in enumerate(DISHES):
            dish = Dish.objects.create(
                category=self.categories[cat], name=name, name_uz=name_uz, price=price,
                description=desc, description_uz=desc_uz, sort_order=order,
                is_available=key not in STOP_LIST, is_hidden=key in HIDDEN,
                prep_minutes=PREP_MINUTES.get(cat, 0), allergens=allergens_for(name),
            )
            dish.modifiers.set([self.modifiers[m] for m in mods])
            self.dishes[key] = dish
            if weight:
                self.weights[key] = weight

    def _audit_log(self, days):
        """История журнала действий. Второй официант отменяет заметно чаще остальных: на нём видно отчёт «Стоит проверить»."""
        from core.models import AuditLog

        A = AuditLog.Action
        users = {u.username: u for u in User.objects.all()}
        rng = random.Random(77)
        orders = list(Order.objects.order_by("-id").values_list("id", flat=True)[:200])

        def add(username, action, ago_hours, amount=None, detail="", order=True):
            user = users.get(username)
            if user is None:
                return
            entry = AuditLog.objects.create(
                user=user, user_name=user.get_full_name() or user.username, action=action, amount=amount,
                order_id=rng.choice(orders) if order and orders else None, detail=detail,
            )
            AuditLog.objects.filter(pk=entry.pk).update(created_at=self.now - timedelta(hours=ago_hours))

        reasons = ["Гость передумал", "Долго ждал", "Ошибка в заказе", "Гость ушёл", "Не та позиция"]
        for day in range(days):
            base = day * 24
            add("kassir", A.SHIFT_OPEN, base + 12, 200000, "Начальный остаток", order=False)
            add("kassir", A.SHIFT_CLOSE, base + 2, 0, "Касса сошлась", order=False)
            if rng.random() < 0.6:
                add("ofitsiant", A.ORDER_CANCEL, base + rng.randint(3, 10), rng.randint(2, 8) * 10000, rng.choice(reasons))
            for _i in range(rng.choice([0, 1, 2, 2, 3])):
                add("ofitsiant2", A.ORDER_CANCEL, base + rng.randint(3, 10), rng.randint(3, 12) * 10000, rng.choice(reasons))
            if rng.random() < 0.5:
                add("ofitsiant2", A.ITEM_REMOVE, base + rng.randint(3, 10), rng.randint(1, 4) * 15000, "Блюдо с кухни убрано")
            if rng.random() < 0.4:
                add("kassir", A.DISCOUNT, base + rng.randint(3, 10), rng.randint(1, 4) * 5000, "Процент 10: постоянный гость")
        add("admin", A.STOCK_WRITEOFF, 30, 18000, "Говядина: 600 г. Испортилась при хранении", order=False)
        add("admin", A.STOCK_COUNT, 50, 42000, "Исправлено позиций: 4, недостача на 42000 сум", order=False)
        add("owner", A.PRICE_CHANGE, 70, 45000, "Плов по-ташкентски: 42000 → 45000", order=False)
        add("kassir", A.REFUND, 20, 85000, "Гостю принесли не то блюдо")
        add("kassir", A.CASH_MOVE, 26, 500000, "Изъятие / Инкассация: сдача выручки", order=False)

    def _guest_data(self):
        """Оценки гостей и баллы за повторные визиты: блок «Отзывы» и карточка гостя на демо не пустые."""
        from core.models import CafeSettings
        from pos import loyalty
        from pos.models import Review

        cafe = CafeSettings.get()
        cafe.loyalty_percent = 5
        cafe.save(update_fields=["loyalty_percent"])
        rng = random.Random(404)
        comments = {
            5: ["Очень вкусно, быстро принесли", "Лучший плов в районе", "Уютно, обязательно вернёмся", ""],
            4: ["Хорошо, но немного шумно", "", "Вкусно, цены чуть выше ожиданий"],
            3: ["Ждали заказ дольше обычного", "Блюдо было тёплым, не горячим"],
            2: ["Долго несли счёт", "Официант забыл напиток"],
            1: ["Суп холодный, пришлось просить подогреть"],
        }
        paid = list(Order.objects.filter(status=Order.Status.PAID).order_by("-closed_at")[:160])
        for order in paid:
            if rng.random() < 0.3:
                rating = rng.choices([5, 4, 3, 2, 1], weights=[52, 26, 12, 7, 3])[0]
                review = Review.objects.create(order=order, rating=rating, comment=rng.choice(comments[rating]))
                Review.objects.filter(pk=review.pk).update(created_at=order.closed_at)
        for order in paid:
            if order.customer_phone:
                loyalty.earn(order)

    def _stock_items(self):
        self.products = {}
        for key, name, name_uz, unit, cost, min_stock in PRODUCTS:
            self.products[key] = Product.objects.create(
                name=name, name_uz=name_uz, unit=unit, cost_per_unit=cost, min_stock=min_stock
            )
        for key, _cat, _n, _nu, _p, _w, recipe, _m, _d, _du in DISHES:
            for product, quantity in recipe.items():
                RecipeItem.objects.create(dish=self.dishes[key], product=self.products[product], quantity=quantity)

    # ---------- заказы ----------

    def _at(self, day, hour, minute=0):
        return timezone.make_aware(datetime.combine(day, time(hour, minute)))

    def _pick_lines(self, order_type):
        rng = self.rng
        keys = list(self.weights)
        weights = [self.weights[k] for k in keys]
        count = {"dine_in": rng.randint(2, 5), "takeaway": rng.randint(1, 3), "delivery": rng.randint(2, 4)}[order_type]
        chosen = []
        for key in rng.choices(keys, weights=weights, k=count):
            if key in [c[0] for c in chosen]:
                continue
            qty = 1
            if key in ("samsa", "kebab_lamb", "kebab_beef", "green_tea", "cola", "water"):
                qty = rng.choice([1, 1, 2, 2, 3, 4])
            elif rng.random() < 0.2:
                qty = 2
            mods = []
            dish = self.dishes[key]
            options = list(dish.modifiers.all())
            groups = {}
            for m in options:
                if m.group:
                    groups.setdefault(m.group, []).append(m)
            for group_options in groups.values():
                mods.append(rng.choice(group_options) if rng.random() < 0.6 else group_options[0])
            extras = [m for m in options if not m.group]
            if extras and rng.random() < 0.2:
                mods.append(rng.choice(extras))
            chosen.append((key, qty, mods))
        return chosen

    def _add_items(self, order, lines, status, sent_at=None, ready_at=None):
        for key, qty, mods, *rest in lines:
            item_status = rest[0] if rest else status
            item = OrderItem.objects.create(
                order=order, dish=self.dishes[key], quantity=qty, status=item_status,
                sent_at=None if item_status == OrderItem.Status.NEW else sent_at,
                ready_at=ready_at if item_status in (OrderItem.Status.READY, OrderItem.Status.SERVED) else None,
            )
            item.modifiers.set(mods)
        order._prefetched_objects_cache = {}

    def _new_order(self, created, order_type, table=None, waiter=None, **fields):
        service = self.cafe.service_percent if order_type == Order.OrderType.DINE_IN else 0
        order = Order.objects.create(
            order_type=order_type, table=table, waiter=waiter or self.rng.choice(self.waiters),
            service_percent=service, **fields,
        )
        Order.objects.filter(pk=order.pk).update(created_at=created)
        order.created_at = created
        return order

    def _pay(self, order, closed, method=None):
        """Оплата как на кассе: способ, получено наличными, сдача. Затем списание по техкартам."""
        rng = self.rng
        method = method or rng.choices(["cash", "card", "mixed"], weights=[45, 45, 10])[0]
        total = order.total
        received = card = None
        if method == "cash":
            step = rng.choice([1000, 5000, 10000, 50000])
            received = -(-int(total) // step) * step if rng.random() < 0.7 else int(total)
        elif method == "mixed":
            card = round_sum(total * Decimal(rng.choice([40, 50, 60])) / 100 / 1000) * 1000
            cash_due = total - card
            received = -(-int(cash_due) // 5000) * 5000
        order.status = Order.Status.PAID
        order.payment_method = method
        order.cash_received = received
        order.card_amount = card
        order.cashier = self.users["kassir"]
        order.closed_at = closed
        order.save()
        write_off_order(order)
        StockMovement.objects.filter(order=order).update(created_at=closed)

    def _history_order(self, created, closed_limit):
        rng = self.rng
        order_type = rng.choices(["dine_in", "takeaway", "delivery"], weights=[65, 25, 10])[0]
        table = self.tables[rng.choice(list(self.tables))] if order_type == "dine_in" else None
        fields = {}
        if order_type == "delivery":
            fields = {
                "customer_name": rng.choice(["Nodira", "Bekzod", "Kamola", "Otabek", "Zarina"]),
                "customer_phone": f"+998 9{rng.randint(0, 9)} {rng.randint(100, 999)} {rng.randint(10, 99)} {rng.randint(10, 99)}",
                "delivery_address": rng.choice(["Chilonzor 9-kvartal, 12-uy", "Yunusobod 4-mavze, 7-uy", "Mirzo Ulugʻbek, Buyuk Ipak yoʻli 55"]),
            }
        r = rng.random()
        if r < 0.07:
            fields.update(discount_kind="percent", discount_value=10, discount_reason="Doimiy mehmon")
        elif r < 0.09:
            fields.update(discount_kind="amount", discount_value=10000, discount_reason="Kutish uchun kompensatsiya")
        order = self._new_order(created, order_type, table, **fields)
        sent = created + timedelta(minutes=rng.randint(1, 4))
        ready = sent + timedelta(minutes=rng.randint(8, 22))
        if rng.random() < 0.02:
            self._add_items(order, self._pick_lines(order_type), OrderItem.Status.NEW)
            order.status = Order.Status.CANCELLED
            order.cancel_reason = rng.choice(CANCEL_REASONS)
            order.cancelled_by = self.users["admin"]
            order.closed_at = created + timedelta(minutes=rng.randint(3, 15))
            order.save()
            return
        self._add_items(order, self._pick_lines(order_type), OrderItem.Status.SERVED, sent, ready)
        duration = rng.randint(25, 75) if order_type == "dine_in" else rng.randint(8, 25)
        closed = min(created + timedelta(minutes=duration), closed_limit)
        if order_type == "dine_in" and rng.random() < 0.7:
            order.bill_requested_at = closed - timedelta(minutes=rng.randint(2, 6))
        self._pay(order, closed)

    def _history(self, days):
        rng = self.rng
        # Продажи по часам: обед и вечер загружены сильнее
        hours = list(range(9, 23))
        hour_weights = [2, 3, 4, 9, 10, 7, 4, 3, 4, 7, 9, 8, 5, 3]
        local_now = timezone.localtime(self.now)
        for offset in range(days, -1, -1):
            day = self.today - timedelta(days=offset)
            weekend = day.weekday() >= 4
            count = rng.randint(40, 52) if weekend else rng.randint(28, 38)
            if offset == 0:
                # Сегодня: только уже прошедшие часы, последний час оставляем под активные заказы
                limit = local_now - timedelta(minutes=70)
                count = int(count * max(0, min(1, (limit.hour + limit.minute / 60 - 9) / 14)))
            for _ in range(count):
                hour = rng.choices(hours, weights=hour_weights)[0]
                created = self._at(day, hour, rng.randint(0, 59))
                if offset == 0 and created > local_now - timedelta(minutes=70):
                    continue
                self._history_order(created, self.now - timedelta(minutes=60))

    def _active_orders(self):
        """Текущая смена: столы в разных состояниях, кухня, навынос и доставка."""
        now = self.now
        ago = lambda m: now - timedelta(minutes=m)  # noqa: E731
        jasur, sardor = self.users["ofitsiant"], self.users["ofitsiant2"]
        m = self.modifiers
        S = OrderItem.Status

        # Стол 1: всё подано, гость попросил счёт
        o = self._new_order(ago(55), "dine_in", self.tables["1"], jasur, bill_requested_at=ago(4))
        self._add_items(o, [("plov", 2, []), ("achichuk", 1, []), ("green_tea", 1, [])], S.SERVED, ago(52), ago(38))
        # Стол 2: шашлык готовится, напитки поданы
        o = self._new_order(ago(25), "dine_in", self.tables["2"], jasur)
        self._add_items(o, [("kebab_lamb", 4, [], S.COOKING), ("lagman", 1, [], S.COOKING), ("cola", 2, [], S.SERVED)],
                        S.COOKING, ago(12), ago(10))
        # Стол 3: только что отправлен на кухню
        o = self._new_order(ago(6), "dine_in", self.tables["3"], sardor)
        self._add_items(o, [("burger", 1, [m["extra_cheese"]]), ("fries", 1, []), ("latte", 1, [m["cup_m"]])],
                        S.QUEUED, ago(3))
        OrderItem.objects.filter(order=o, dish=self.dishes["burger"]).update(comment="Piyozsiz")
        # Стол 4: блюда готовы, официант должен подать
        o = self._new_order(ago(35), "dine_in", self.tables["4"], jasur)
        self._add_items(o, [("manti", 1, [], S.READY), ("samsa", 3, [], S.READY), ("black_tea", 1, [], S.SERVED)],
                        S.READY, ago(20), ago(2))
        # Стол 5: официант набирает заказ, ещё не отправил
        o = self._new_order(ago(3), "dine_in", self.tables["5"], sardor)
        self._add_items(o, [("caesar", 1, []), ("cappuccino", 2, [m["cup_s"]])], S.NEW)
        # Стол 6: бронь на вечер
        table = self.tables["6"]
        reserve_at = timezone.localtime(now).replace(hour=19, minute=30, second=0, microsecond=0)
        if reserve_at < now:
            reserve_at = timezone.localtime(now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        table.reserved_for, table.reserved_at = "Rustam aka, 6 kishi", reserve_at
        table.save()
        # Терраса: один заказ ждёт слишком долго (подсветка на кухне)
        o = self._new_order(ago(40), "dine_in", self.tables["T1"], jasur)
        self._add_items(o, [("steak", 1, []), ("greek", 1, [])], S.COOKING, ago(33))
        OrderItem.objects.filter(order=o, dish=self.dishes["steak"]).update(comment="Medium well")
        o = self._new_order(ago(15), "dine_in", self.tables["T3"], sardor)
        self._add_items(o, [("pepperoni_pizza", 1, [m["pizza_40"]], S.COOKING), ("lemonade", 2, [m["lemon_straw"]], S.SERVED)],
                        S.COOKING, ago(10), ago(8))
        # Навынос: оплачен картой, ждёт кухню
        o = self._new_order(ago(8), "takeaway", None, self.users["kassir"], customer_name="Bekzod")
        self._add_items(o, [("cheeseburger", 2, []), ("fries", 2, []), ("cola", 2, [])], S.QUEUED, ago(7))
        self._pay_active(o, ago(7), "card")
        # Навынос: оплачен наличными, готов к выдаче
        o = self._new_order(ago(20), "takeaway", None, self.users["kassir"], customer_name="Kamola")
        self._add_items(o, [("cappuccino", 2, [m["cup_m"], m["syrup"]]), ("cheesecake", 2, [])], S.READY, ago(19), ago(3))
        self._pay_active(o, ago(19), "cash")
        # Доставка: оплата при получении, готовится
        o = self._new_order(
            ago(18), "delivery", None, self.users["kassir"], customer_name="Nodira",
            customer_phone="+998 90 123 45 67", delivery_address="Chilonzor 9-kvartal, 12-uy, 45-xonadon",
            note="Domofon ishlamaydi, qoʻngʻiroq qiling",
        )
        self._add_items(o, [("plov", 3, []), ("achichuk", 2, []), ("samsa", 4, [])], S.COOKING, ago(15))
        # Заявка гостя по QR-коду со стола 7
        guest = GuestOrder.objects.create(table=self.tables["7"])
        GuestOrder.objects.filter(pk=guest.pk).update(created_at=ago(2))
        GuestOrderItem.objects.create(guest_order=guest, dish=self.dishes["plov"], quantity=2)
        GuestOrderItem.objects.create(guest_order=guest, dish=self.dishes["green_tea"], quantity=1)

    def _pay_active(self, order, closed, method):
        """Оплаченный навынос: деньги приняты, кухня ещё работает."""
        total = order.total
        order.status = Order.Status.PAID
        order.payment_method = method
        order.cash_received = -(-int(total) // 10000) * 10000 if method == "cash" else None
        order.cashier = self.users["kassir"]
        order.closed_at = closed
        order.save()
        write_off_order(order)
        StockMovement.objects.filter(order=order).update(created_at=closed)

    # ---------- склад ----------

    def _stock_income(self, days):
        """Приход товара задним числом, чтобы итоговые остатки были правдоподобными.
        Расход уже записан списаниями по продажам; здесь закупка = расход + желаемый остаток."""
        rng = self.rng
        admin = self.users["admin"]
        first_day = self.today - timedelta(days=days + 1)
        mid_day = self.today - timedelta(days=days // 2)
        writeoffs = {"tomato": (Decimal(1500), "Buzilgan / Испорчено"), "milk": (Decimal(2000), "Muddati oʻtgan / Истёк срок")}
        for key, product in self.products.items():
            used = -(StockMovement.objects.filter(product=product, kind=StockMovement.Kind.SALE)
                     .aggregate(s=Sum("quantity"))["s"] or Decimal(0))
            lost = writeoffs.get(key, (Decimal(0), ""))[0]
            if key in LOW_STOCK:
                target = (product.min_stock * Decimal("0.6")).quantize(Decimal("1"))
            else:
                target = (product.min_stock * Decimal(rng.uniform(1.6, 3.2))).quantize(Decimal("1"))
            total_in = used + lost + target
            step = Decimal(10) if product.unit == Product.Unit.PIECE else Decimal(1000)
            first = ((total_in * Decimal("0.55")) / step).quantize(Decimal("1")) * step
            second = total_in - first
            if key not in LOW_STOCK:
                second = (second / step).to_integral_value(rounding=ROUND_CEILING) * step
            for day, qty, note in ((first_day, first, "Korzinka Opt · №{}".format(rng.randint(100, 999))),
                                   (mid_day, second, "Korzinka Opt · №{}".format(rng.randint(100, 999)))):
                if qty <= 0:
                    continue
                movement = StockMovement.objects.create(
                    product=product, kind=StockMovement.Kind.INCOME, quantity=qty, comment=note, created_by=admin
                )
                StockMovement.objects.filter(pk=movement.pk).update(created_at=self._at(day, 8, rng.randint(0, 50)))
            if lost:
                movement = StockMovement.objects.create(
                    product=product, kind=StockMovement.Kind.WRITEOFF, quantity=lost, comment=writeoffs[key][1],
                    created_by=admin,
                )
                StockMovement.objects.filter(pk=movement.pk).update(created_at=self.now - timedelta(days=2, hours=3))

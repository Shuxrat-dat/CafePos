"""Порядок блюд nihol как в прежней кассе Zim-Zim (по снимкам экрана владельца, ряды слева направо).

Запуск на сервере из папки программы:
    python manage.py shell -c "exec(open('scripts/nihol_menu_order.py').read()); run(apply=False)"
apply=False только показывает, что изменится; apply=True записывает sort_order.
Блюда, которых нет в списке, встают в конец своей категории по алфавиту.
"""

ORDER = {
    "BIRINCHI": ["Mastava", "Shurva", "Borsh", "Bulyon", "Tuy kabob"],
    "IKKINCHI": [
        "TANDIR KABOB", "Somsa", "Tovuq tandir kg", "Jiz kg",
        "kantenr", "Tabaka", "Jaz kg", "Kartoshka fri 1kg",
        "Kartoshka fri 0.5kg", "Go'sht assorti", "Baliq", "Viona tarxun",
    ],
    "SHASHLIK": [
        "Molotoy shashlik", "Kuskavoy (mol go'sht)", "Kuskavoy (qo'y go'sht)", "non(shashlik)",
        "Jigar shashlik", "Shashlik ( dumba)", "Tovuq shashlik", "Napalyon",
        "Dumba", "Farsh", "Rulet shashlik", "Pamidor shashlik",
        "Biqin", "Steyk", "URDAK SHASHLIK", "Katta shashlik",
    ],
    "NON-CHOY": [
        "Non (katta)", "Chap chak Non", "Qora Non", "Non (yarimta)",
        "Patir (0.25)", "Non (bo'lak)", "Choy", "Limon choy",
        "NON",
    ],
    "SALAT": [
        "Qalampir", "salat", "Sveji salat", "Chiroqchi salat",
        "Chakki", "Zelen (mayda)", "Zelen (katta)", "Sovus",
        "Limon", "Sheyx salat", "Halades", "Salyonni",
        "Salyonni bodiring", "Qatiq", "Smak", "Baqlajon salat",
        "Mujskoy kapriz", "Venegret", "Fransuski salat", "Vecher salat",
        "Majnuntol", "Olivia", "Quziqorin", "Mevali salat",
        "Sezar salat", "Nihol salat",
    ],
    "ICHIMLIK": [
        "Coca-Cola 1.5 L", "Coca Cola 1 L", "Coca Cola 0.5 L", "Fanta 1.5 L",
        "Kampot", "Fanta 1L", "chalob", "Tarxun",
        "Fanta 0.5 L", "Pepsi 1.5 L", "Pepsi 1 L", "ALOE",
        "chortoq 0.5", "Pepsi 0.5 L", "barjomi", "ESSENTUK (4)",
        "Flash", "Adrenalin", "Moxito", "Dinay",
        "Sok viko", "Sok sochnaya dalina", "Suv gaz 1L", "Suv gaz 0.5l",
        "Suv gaz 1.5L", "Suv bez gaz 0.5", "Suz bez gaz 1l", "Suv 1.5L",
        "Gorilla", "Saber energetic", "Elevia", "BLANC BLEU",
        "KARLOVY VARY", "Flavis 0.5", "Flavis 1l", "Raya moxita",
        "Nataxtari", "Pear",
    ],
    "AROQ": [
        "medved", "Chistaya", "baykal 0.7", "new uzb 0.7",
        "good goose", "Baikal aroq 0.5l", "Svayak", "aroq 50 g",
        "Chekushka aroq 200g", "Marmar", "100gr Aroq", "stary samarqand 0,5",
        "NEW Uzbekistan", "R0SA 0.7", "TIGR 0.5", "TIGR 0,7",
        "Silk Road", "Stary Samarknd 0,7",
    ],
    "KONYAK": [
        "Stara Armeniya", "Shokolad", "Vino", "Samarkand konyak 0.5 L",
        "Konyak (tanbur)", "Guduary (qizil)", "Chateau Hamkor", "Shustoff Grand Royal 0.5",
        "Weski", "Kogor", "Jack Daniel’s", "Shustoff 0.5l",
        "Bagizagan", "Konyak", "Chokolatier", "Samarqand konyak",
        "Old Uzbekistan", "Tanbour", "NAPALEON", "JACKSON",
        "ARMENIYA 0.7", "Nairi", "Samarkand 0.25", "ARMI'AN 0.25",
        "STARY Samarkand 0.5", "STARY Samarkand 0.7", "Filatoff 1868 0.5", "NEW Samarkand",
        "BARLAS", "Samarkand shokolad 0,5", "chanteau de louis", "BOGIZAGON",
    ],
    "PIVO": [
        "Pulsar 0.5L", "Tuborg 0,45 can", "Baltika", "Sarbast 0.5L",
        "Pivo 1.5L", "Pulsar 1L", "Carlsberg Pilsner 0,45", "Tuborg geen RGB 0,5",
    ],
    "SIGARIT": ["Dona sigaret", "Pall mall mix", "Kent pachka", "Parlament", "Essi chinch", "zajigalka"],
    "CHAQ CHUQ": ["Donak", "Qurt", "Sirniy palochka", "Semichka", "Orbit", "kantenir"],
    "DESERT": [
        "Muzqaymoq", "Muzqaymoq quyma", "Meva", "Tarvuz",
        "perojne", "Musqaymoq", "Musqaynoq", "musqaymoq gr 100",
    ],
}


def run(apply=False):
    from menu.models import Category, Dish

    changed, missing = 0, []
    for cat_name, names in ORDER.items():
        category = Category.objects.filter(name=cat_name).first()
        if category is None:
            missing.append(f"категория {cat_name}")
            continue
        dishes = list(Dish.objects.filter(category=category))
        by_name = {d.name: d for d in dishes}
        ordered = []
        for name in names:
            dish = by_name.pop(name, None)
            if dish is None:
                missing.append(f"{cat_name}: {name}")
            else:
                ordered.append(dish)
        ordered += sorted(by_name.values(), key=lambda d: d.name.lower())  # не указанные — в конец
        for position, dish in enumerate(ordered, start=1):
            if dish.sort_order != position:
                changed += 1
                if apply:
                    Dish.objects.filter(id=dish.id).update(sort_order=position)
        print(cat_name, "->", ", ".join(d.name for d in ordered))
    print("Не найдено:", missing or "—")
    print(("Записано" if apply else "Будет изменено"), changed, "блюд")

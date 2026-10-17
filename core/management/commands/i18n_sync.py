from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core import i18n_tools


class Command(BaseCommand):
    help = (
        "Собирает строки интерфейса из шаблонов и кода, дописывает новые в locale/*/django.po и собирает .mo. "
        "С ключом --check только проверяет, что узбекский перевод есть у каждой строки."
    )

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Ничего не менять, завершиться с ошибкой при пропусках")

    def handle(self, *args, **options):
        root = settings.BASE_DIR
        sources = i18n_tools.extract_all(root)
        uz_path = root / "locale" / "uz" / "LC_MESSAGES" / "django.po"
        ru_path = root / "locale" / "ru" / "LC_MESSAGES" / "django.po"
        uz = i18n_tools.parse_po(uz_path)

        missing = sorted(s for s in sources if not uz.get(s))
        if options["check"]:
            if missing:
                lines = "\n".join(f"  {s[:90]}   ({sources[s][0]})" for s in missing[:30])
                raise CommandError(f"Нет узбекского перевода у {len(missing)} строк:\n{lines}")
            self.stdout.write(self.style.SUCCESS(f"Перевод полный: {len(sources)} строк."))
            return

        for s in missing:
            uz.setdefault(s, "")
        i18n_tools.write_po(uz_path, uz, "uz")
        i18n_tools.compile_mo(uz, "uz", uz_path.with_suffix(".mo"))

        # Исходный язык русский, поэтому каждая строка переводится сама в себя. Пустой перевод нельзя:
        # тогда Django возьмёт запасной узбекский каталог и на русском экране появится узбекский текст.
        ru = i18n_tools.parse_po(ru_path) if ru_path.exists() else {}
        for s in {*sources, *uz}:
            if not ru.get(s):
                ru[s] = s
        i18n_tools.write_po(ru_path, ru, "ru")
        i18n_tools.compile_mo(ru, "ru", ru_path.with_suffix(".mo"))

        note = f", без перевода: {len(missing)}" if missing else ""
        self.stdout.write(self.style.SUCCESS(f"Строк в проекте: {len(sources)}{note}. Файлы .mo пересобраны."))

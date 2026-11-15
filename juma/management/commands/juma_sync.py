import threading

from django.core.management.base import BaseCommand, CommandError

from juma import client, worker


class Command(BaseCommand):
    help = "Синхронизация кассы с облаком Juma: связь, каталог, заказы. Запускайте один экземпляр (служба или отдельное окно)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Выполнить один проход и выйти (проверка настроек).")

    def handle(self, *args, **options):
        if not client.enabled():
            raise CommandError("Juma не настроена: задайте JUMA_URL и JUMA_MERCHANT_KEY в файле .env.")
        if options["once"]:
            ok = worker.heartbeat_once()
            self.stdout.write("Связь с Juma: " + ("есть" if ok else "нет (см. страницу «Juma» на кассе)"))
            return
        stop = threading.Event()
        try:
            worker.run(stop)
        except KeyboardInterrupt:
            stop.set()

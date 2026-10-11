import glob
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

KEEP = 30  # сколько последних копий каждого типа хранить


def _find_pg_dump():
    path = shutil.which("pg_dump")
    if path:
        return path
    found = sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin\pg_dump.exe"))
    if found:
        return found[-1]
    raise CommandError("Не найден pg_dump. Установите PostgreSQL Command Line Tools.")


def _prune(folder, prefix, suffix):
    files = sorted(glob.glob(os.path.join(folder, f"{prefix}*{suffix}")))
    for old in files[:-KEEP]:
        os.remove(old)


class Command(BaseCommand):
    help = "Резервная копия базы (SQLite: копия файла, PostgreSQL: pg_dump)"

    def handle(self, *args, **options):
        folder = os.path.join(settings.BASE_DIR, "backups")
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        db = settings.DATABASES["default"]

        if db["ENGINE"].endswith("sqlite3"):
            target = os.path.join(folder, f"db-{stamp}.sqlite3")
            source = sqlite3.connect(str(db["NAME"]))
            copy = sqlite3.connect(target)
            with copy:
                source.backup(copy)
            copy.close()
            source.close()
            _prune(folder, "db-", ".sqlite3")
        elif db["ENGINE"].endswith("postgresql"):
            target = os.path.join(folder, f"pg-{stamp}.dump")
            env = dict(os.environ, PGPASSWORD=str(db.get("PASSWORD", "")))
            command = [
                _find_pg_dump(), "-U", str(db["USER"]), "-h", str(db.get("HOST") or "localhost"),
                "-p", str(db.get("PORT") or "5432"), "-d", str(db["NAME"]), "-F", "c", "-f", target,
            ]
            result = subprocess.run(command, env=env, capture_output=True)
            if result.returncode != 0:
                if os.path.exists(target):
                    os.remove(target)
                message = result.stderr.decode("utf-8", errors="replace").strip()
                raise CommandError(f"pg_dump завершился с ошибкой: {message}")
            _prune(folder, "pg-", ".dump")
        else:
            raise CommandError("Эта база не поддерживается бэкапом.")

        self.stdout.write(self.style.SUCCESS(f"Копия создана: {target}"))
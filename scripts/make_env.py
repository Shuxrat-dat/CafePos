"""Создаёт .env для рабочей кассы или дописывает в него текущий IP ноутбука.

Запускается из install.bat. Django здесь не импортируется: без .env настройки не загрузятся.
"""

import secrets
import socket
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
HOSTS_KEY = "DJANGO_ALLOWED_HOSTS"


def local_ips():
    """IPv4-адреса ноутбука в локальной сети (без 127.x)."""
    ips = []
    try:
        # UDP connect ничего не отправляет, только выбирает сетевой интерфейс по умолчанию.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.0.1", 80))
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


def wanted_hosts():
    return ["127.0.0.1", "localhost", socket.gethostname().lower(), *local_ips()]


def create_env():
    hosts = ",".join(dict.fromkeys(wanted_hosts()))
    ENV_PATH.write_text(
        "# Создан install.bat. Файл .env в git не попадает.\n"
        f"DJANGO_SECRET_KEY={secrets.token_urlsafe(50)}\n"
        "DJANGO_DEBUG=False\n"
        f"{HOSTS_KEY}={hosts}\n"
        "# Включайте только если сайт открывается по https\n"
        "DJANGO_HTTPS=False\n",
        encoding="utf-8",
    )
    print(".env created")


def add_missing_hosts():
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{HOSTS_KEY}="):
            current = [h.strip() for h in line.split("=", 1)[1].split(",") if h.strip()]
            missing = [h for h in wanted_hosts() if h not in current]
            if missing:
                lines[i] = f"{HOSTS_KEY}={','.join(current + missing)}"
                ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
                print(f".env: added {', '.join(missing)}")
            else:
                print(".env already exists, kept as is")
            return
    lines.append(f"{HOSTS_KEY}={','.join(dict.fromkeys(wanted_hosts()))}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f".env: added {HOSTS_KEY}")


def main():
    if ENV_PATH.exists():
        add_missing_hosts()
    else:
        create_env()
    ips = local_ips()
    if ips:
        # Первым идёт адрес основного подключения; остальные бывают у виртуальных адаптеров.
        print(f"Address for phones and tablets: http://{ips[0]}:8000")
    else:
        print("No local network found. Connect the laptop to the cafe Wi-Fi and run install.bat again.")


if __name__ == "__main__":
    main()

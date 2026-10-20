"""CafePOS: программа печати для компьютера в кафе.

Касса работает через интернет и складывает чеки и кухонные заказы в очередь. Эта программа
каждые несколько секунд спрашивает кассу о новых заданиях и отправляет готовые ESC/POS-данные
на термопринтеры:
  * по сети (LAN) — TCP-подключение к IP-адресу и порту принтера (у сетевых термопринтеров обычно 9100);
  * по USB — через принтер, установленный в Windows с драйвером производителя (нужен модуль pywin32).

Нужен только Python 3.10+. Настройки — в файле cafepos_print_agent.ini рядом с программой
(его кладёт в архив страница «Настройки → Принтеры»).

Команды:
  py cafepos_print_agent.py                 работать (забирать и печатать задания)
  py cafepos_print_agent.py --list          показать принтеры, установленные в Windows
  py cafepos_print_agent.py --test-ip 192.168.1.200[:9100]
                                            напечатать пробную строку прямо на сетевой принтер
"""
import argparse
import base64
import configparser
import json
import logging
import socket
import sys
import time
import urllib.error
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

VERSION = "1.0"
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "cafepos_print_agent.ini"
LOG_PATH = HERE / "cafepos_print_agent.log"
SOCKET_TIMEOUT = 10
CHECK_TIMEOUT = 3
CONNECT_ATTEMPTS = 3
CONNECT_RETRY_DELAY = 2
HTTP_TIMEOUT = 20
MAX_BACKOFF = 60

log = logging.getLogger("cafepos_print_agent")


class PrintError(Exception):
    """Ошибка печати: code уходит на кассу, а текст для людей касса пишет на своём языке."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---------- отправка на принтер ----------

def connect(host, port, timeout=SOCKET_TIMEOUT, attempts=1):
    """Подключение к сетевому принтеру. Принтер, который только что включили или который занят
    другим заданием, может не ответить сразу, поэтому подключение повторяется."""
    error = None
    for attempt in range(attempts):
        try:
            return socket.create_connection((host, int(port)), timeout=timeout)
        except socket.timeout:
            error = PrintError("timeout", f"{host}:{port}")
        except ConnectionRefusedError:
            error = PrintError("refused", f"{host}:{port}")
        except OSError as exc:
            error = PrintError("network", f"{host}:{port} {exc.strerror or exc}")
        if attempt + 1 < attempts:
            time.sleep(CONNECT_RETRY_DELAY)
    raise error


def send_network(host, port, data):
    # Повторяем только подключение: пока соединения нет, на принтер ничего не ушло и дубля не будет
    with connect(host, port, attempts=CONNECT_ATTEMPTS) as conn:
        try:
            conn.sendall(data)
        except OSError as exc:
            raise PrintError("network", f"{host}:{port} {exc.strerror or exc}")


def check_printer(printer):
    """Проверка связи без печати: подключиться к принтеру и сразу отключиться."""
    if printer.get("connection") == "network":
        with connect(printer.get("host", ""), printer.get("port", 9100), timeout=CHECK_TIMEOUT, attempts=2):
            pass
    elif printer.get("connection") == "windows":
        try:
            import win32print
        except ImportError:
            raise PrintError("no_pywin32")
        try:
            win32print.ClosePrinter(win32print.OpenPrinter(printer.get("windows_name", "")))
        except Exception as exc:
            raise PrintError("not_found", f"{printer.get('windows_name', '')} ({exc})")
    else:
        raise PrintError("bad_connection", str(printer.get("connection")))


def send_windows(name, data):
    try:
        import win32print  # из пакета pywin32
    except ImportError:
        raise PrintError("no_pywin32")
    try:
        handle = win32print.OpenPrinter(name)
    except Exception as exc:
        raise PrintError("not_found", f"{name} ({exc})")
    try:
        # Тип RAW: данные ESC/POS уходят на принтер как есть, без обработки драйвером
        win32print.StartDocPrinter(handle, 1, ("CafePOS", None, "RAW"))
        try:
            win32print.StartPagePrinter(handle)
            win32print.WritePrinter(handle, data)
            win32print.EndPagePrinter(handle)
        finally:
            win32print.EndDocPrinter(handle)
    except Exception as exc:
        raise PrintError("windows", f"{name} ({exc})")
    finally:
        win32print.ClosePrinter(handle)


def send(printer, data):
    if printer.get("connection") == "network":
        send_network(printer.get("host", ""), printer.get("port", 9100), data)
    elif printer.get("connection") == "windows":
        send_windows(printer.get("windows_name", ""), data)
    else:
        raise PrintError("bad_connection", str(printer.get("connection")))


def list_windows_printers():
    try:
        import win32print
    except ImportError:
        print("Нужен модуль pywin32: py -m pip install pywin32")
        return 1
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    names = [entry[2] for entry in win32print.EnumPrinters(flags)]
    print("Принтеры в Windows (впишите имя в «Настройки → Принтеры»):")
    for name in names:
        print(f"  {name}")
    return 0


def test_ip(target):
    host, _, port = target.partition(":")
    data = b"\x1b@" + b"CafePOS print agent: test OK\n" + host.encode() + b"\n\n\n\n" + b"\x1dV\x42\x00"
    try:
        send_network(host, port or 9100, data)
    except PrintError as exc:
        print(f"Ошибка: {exc}")
        return 1
    print("Отправлено. Если лист не вышел, проверьте IP и порт на странице самотестирования принтера.")
    return 0


# ---------- связь с кассой ----------

class Server:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token
        host = socket.gethostname().encode("ascii", "replace").decode()
        self.info = f"{host} v{VERSION}"

    def _call(self, method, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.url + path, data=body, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "X-Agent-Info": self.info,
            "Content-Type": "application/json",
            "User-Agent": f"CafePOS-print-agent/{VERSION}",
        })
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read() or b"{}")

    def poll(self):
        """Задания на печать и список принтеров, связь с которыми надо проверить."""
        answer = self._call("GET", "/print-agent/jobs/")
        return answer.get("jobs", []), answer.get("check", [])

    def send_check(self, results):
        try:
            self._call("POST", "/print-agent/status/", {"printers": results})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("Не удалось отправить кассе проверку связи: %s", exc)

    def report(self, job_id, ok, code="", detail=""):
        payload = {"ok": ok, "code": code, "detail": detail[:200]}
        for attempt in range(3):
            try:
                self._call("POST", f"/print-agent/jobs/{job_id}/result/", payload)
                return
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log.warning("Не удалось сообщить кассе результат задания %s: %s", job_id, exc)
                time.sleep(2 * (attempt + 1))


def process(server, job):
    printer = job.get("printer", {})
    try:
        send(printer, base64.b64decode(job.get("data", "")))
    except PrintError as exc:
        log.error("Ошибка: %s → %s: %s", job.get("title"), printer.get("name"), exc)
        server.report(job["id"], False, exc.code, exc.detail)
    else:
        log.info("Отправлено на принтер: %s → %s", job.get("title"), printer.get("name"))
        server.report(job["id"], True)


def check_all(server, printers):
    results = []
    for printer in printers:
        try:
            check_printer(printer)
        except PrintError as exc:
            results.append({"id": printer.get("id"), "ok": False, "code": exc.code, "detail": exc.detail[:200]})
            log.warning("Нет связи с принтером %s (%s): %s", printer.get("name"), printer.get("host") or printer.get("windows_name"), exc)
        else:
            results.append({"id": printer.get("id"), "ok": True})
    server.send_check(results)


def run(config):
    url = config.get("server", "url", fallback="").strip()
    token = config.get("server", "token", fallback="").strip()
    if not url or not token:
        log.error("В %s не указаны url и token. Скачайте программу заново в «Настройки → Принтеры».", CONFIG_PATH.name)
        return 1
    interval = max(1.0, config.getfloat("agent", "interval", fallback=2.0))
    server = Server(url, token)
    log.info("Программа печати %s запущена. Касса: %s", VERSION, server.url)
    backoff = interval
    while True:
        try:
            jobs, to_check = server.poll()
            backoff = interval
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                log.error("Касса не приняла токен. Скачайте программу заново или обновите token в %s.", CONFIG_PATH.name)
                time.sleep(30)
                continue
            log.warning("Касса ответила ошибкой %s, повтор через %s с", exc.code, int(backoff))
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("Нет связи с кассой (%s), повтор через %s с", exc, int(backoff))
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue
        for job in jobs:
            process(server, job)
        if to_check:
            check_all(server, to_check)
        if not jobs:
            time.sleep(interval)


def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)


def main(argv=None):
    parser = argparse.ArgumentParser(description="CafePOS: программа печати чеков и кухонных заказов")
    parser.add_argument("--list", action="store_true", help="показать принтеры Windows")
    parser.add_argument("--test-ip", metavar="IP[:PORT]", help="пробная печать на сетевой принтер")
    args = parser.parse_args(argv)
    if args.list:
        return list_windows_printers()
    if args.test_ip:
        return test_ip(args.test_ip)
    setup_logging()
    config = configparser.ConfigParser()
    if not config.read(CONFIG_PATH, encoding="utf-8"):
        log.error("Нет файла настроек %s. Скачайте программу в «Настройки → Принтеры».", CONFIG_PATH)
        return 1
    try:
        return run(config)
    except KeyboardInterrupt:
        log.info("Остановлено.")
        return 0


if __name__ == "__main__":
    sys.exit(main())

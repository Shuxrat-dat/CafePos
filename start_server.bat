@echo off
cd /d D:\cafepos
.venv\Scripts\python.exe manage.py collectstatic --noinput
.venv\Scripts\python.exe -m waitress --listen=0.0.0.0:8000 config.wsgi:application
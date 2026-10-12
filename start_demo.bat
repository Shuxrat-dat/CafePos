@echo off
rem CafePOS: demo mode for presentations.
rem Uses a separate database demo.sqlite3 and folder media_demo. The working database is not touched.
cd /d "%~dp0"
set CAFEPOS_DEMO=1
set PYTHONIOENCODING=utf-8
if "%DJANGO_ALLOWED_HOSTS%"=="" set DJANGO_ALLOWED_HOSTS=*
if not exist .venv\Scripts\python.exe (
  py -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
.venv\Scripts\python.exe manage.py migrate --noinput
rem Fills the demo restaurant only if demo.sqlite3 is empty. Reset: Settings - Restore demo data.
.venv\Scripts\python.exe manage.py seed_demo --if-empty
.venv\Scripts\python.exe manage.py collectstatic --noinput -v 0
echo.
echo CafePOS demo: http://127.0.0.1:8000
echo.
.venv\Scripts\python.exe -m waitress --listen=0.0.0.0:8000 config.wsgi:application

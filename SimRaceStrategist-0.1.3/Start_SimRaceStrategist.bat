@echo off
setlocal

cd /d "%~dp0"

REM venv aktivieren
call ".venv\Scripts\activate.bat"

REM App starten
python -m app.main

pause

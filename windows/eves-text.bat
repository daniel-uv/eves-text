@echo off
rem Запуск Eve's Text с консолью — для диагностики.
cd /d "%~dp0"
"%~dp0bin\python.exe" "%~dp0app\launcher.py" %*
if errorlevel 1 pause

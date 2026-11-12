@echo off
rem GoodMorningKOITA (CMD window / debug mode, no tray icon). Usual way: double-click GoodMorningKOITA.vbs (tray icon, no CMD window).
rem run.bat --setup : only create .venv and install libraries (used by GoodMorningKOITA.vbs on first run)
cd /d "%~dp0"
if not exist .venv (
    echo [1/3] Creating virtual environment...
    python -m venv .venv || goto :error
)
echo [2/3] Checking libraries...
.venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt || goto :error
copy /y requirements.txt .venv\requirements.installed.txt >nul
if /i "%~1"=="--setup" (
    echo Setup done. Starting GoodMorningKOITA...
    exit /b 0
)
echo [3/3] Starting server at http://127.0.0.1:8000  (debug mode: close this window to stop)
.venv\Scripts\python app.py
goto :eof
:error
echo.
echo Setup failed. Check that Python 3.10+ is installed and on PATH.
pause
exit /b 1

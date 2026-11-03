@echo off
setlocal
set "APP_DIR=%~dp0"
cd /d "%APP_DIR%"

where py >nul 2>nul
if not errorlevel 1 (
    py -3 main.py
    if errorlevel 1 pause
    exit /b
)

where python >nul 2>nul
if not errorlevel 1 (
    python main.py
    if errorlevel 1 pause
    exit /b
)

echo Python 3 was not found. Install Python 3 and try again.
pause
exit /b 1

@echo off
rem Double-click to run iPod Manager on Windows.
rem First run creates a local .venv and installs dependencies.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Setting up Python environment...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo.
        echo Python 3 was not found. Install it from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH" during setup.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
)

where ffmpeg >nul 2>nul
if errorlevel 1 if not exist "resources\ffmpeg.exe" (
    echo Note: ffmpeg not found - FLAC/OGG/WMA/etc. won't be converted.
    echo       Install it with:  winget install Gyan.FFmpeg
    echo       or put ffmpeg.exe in the "resources" folder.
    echo.
)

start "" http://127.0.0.1:5757
".venv\Scripts\python.exe" app.py
pause

@echo off
rem Двоен клик, за да стартираш агента — без терминал, без Python команди наизуст.
rem "Ниво 0" от предложението за по-лесна употреба (2026-08-12): виж README.md.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Първо стартиране — подготвям обкръжението, това е еднократно...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo Грешка: не намирам Python. Инсталирайте Python 3.12+ от python.org и опитайте пак.
        pause
        exit /b 1
    )
    call .venv\Scripts\activate.bat
    pip install -e ".[dev]" --quiet
) else (
    call .venv\Scripts\activate.bat
)

if not exist ".env" (
    echo .env липсва — агентът ще предложи да го създаде при стартиране.
)

python -m src.cli

echo.
echo Сесията приключи. Натиснете произволен клавиш, за да затворите прозореца.
pause >nul

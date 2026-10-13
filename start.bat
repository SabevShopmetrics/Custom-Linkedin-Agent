@echo off
chcp 65001 >nul
rem Двоен клик, за да стартираш агента — без терминал, без Python команди наизуст.
rem "Ниво 0" от предложението за по-лесна употреба (2026-08-12): виж README.md.
rem research-plan-36, А4 (2026-10-09): кирилицата вече се чете (chcp 65001), версията на
rem Python се проверява, а неуспешна инсталация не оставя счупена папка .venv завинаги.

cd /d "%~dp0"

rem --- кой Python: първо "py -3" (инсталаторът от python.org го слага винаги), после "python"
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
    python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo.
    echo Не намирам Python на този компютър.
    echo Инсталирайте Python 3.12 или по-нов от https://www.python.org/downloads/
    echo и при инсталацията отметнете "Add python.exe to PATH". После стартирайте пак.
    echo.
    pause
    exit /b 1
)

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
if errorlevel 1 (
    echo.
    echo Python на този компютър е по-стар от 3.12. Инсталирайте по-нов от
    echo https://www.python.org/downloads/ и стартирайте пак.
    echo.
    pause
    exit /b 1
)

rem --- обкръжението се смята за готово САМО ако инсталацията е завършила (файлът-маркер)
if not exist ".venv\.installed" (
    echo Първо стартиране — подготвям агента. Отнема 1-3 минути и се прави само веднъж...
    if exist ".venv" rmdir /s /q ".venv"
    %PY% -m venv .venv
    if errorlevel 1 goto install_failed
    call .venv\Scripts\activate.bat
    python -m pip install --disable-pip-version-check -e ".[dev]"
    if errorlevel 1 goto install_failed
    echo ok> ".venv\.installed"
    echo Готово.
) else (
    call .venv\Scripts\activate.bat
)

rem Аргументите се подават нататък — start_web.bat вика "start.bat web --open".
python -m src.cli %*

echo.
echo Сесията приключи. Натиснете произволен клавиш, за да затворите прозореца.
pause >nul
exit /b 0

:install_failed
echo.
echo Подготовката не успя (често заради липса на интернет или прокси).
echo Проверете връзката и стартирайте отново — ще опитам наново от начало.
if exist ".venv" rmdir /s /q ".venv"
echo.
pause
exit /b 1

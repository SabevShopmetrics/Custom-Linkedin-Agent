@echo off
chcp 65001 >nul
rem Двоен клик: уеб интерфейсът на агента (бриф, снимки и ZIP, преглед) се отваря в браузъра.
rem Същата подготовка като start.bat (Python, .venv) — само командата е "web". Прозорецът
rem трябва да остане отворен, докато ползвате интерфейса; затварянето му спира сървъра.
rem Рестарт = рестарт: спира всеки стар сървър на агента (и невидим, пуснат от друг прозорец),
rem иначе на Windows той продължава да отговаря на същия порт (web-ui-5, 2026-10-14).
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*-m src.cli web*' } | ForEach-Object { Write-Host ('Спирам стария сървър на агента (процес ' + $_.ProcessId + ')...'); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
call "%~dp0start.bat" web --open

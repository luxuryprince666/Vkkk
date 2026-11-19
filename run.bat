@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul && (py run.py %* & goto :end)
where python >nul 2>nul && (python run.py %* & goto :end)
echo Python 3 не найден. Скачайте с python.org и отметьте "Add to PATH".
pause
:end
if errorlevel 1 pause

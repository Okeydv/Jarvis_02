@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Диагностика Джарвиса
if not exist ".venv\Scripts\python.exe" (
  echo Сначала запустите install.bat
  pause
  exit /b 1
)
".venv\Scripts\python.exe" main.py --check
echo.
echo Отчёт сохранён в logs\diagnostics.txt
pause

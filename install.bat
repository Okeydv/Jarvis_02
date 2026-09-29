@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Установка Джарвиса

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY goto :no_python
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" || goto :old_python

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] Создаю виртуальное окружение .venv
  %PY% -m venv .venv || goto :error
)
set "VPY=.venv\Scripts\python.exe"

echo [2/4] Устанавливаю PyTorch для процессора - это самый большой пакет, подождите
"%VPY%" -m pip install --upgrade pip
"%VPY%" -m pip install torch --index-url https://download.pytorch.org/whl/cpu || goto :error

echo [3/4] Устанавливаю остальные зависимости
"%VPY%" -m pip install -r requirements.txt || goto :error

echo [4/4] Скачиваю модели Vosk и Silero и сертификат для GigaChat
"%VPY%" download_models.py

if not exist ".env" copy ".env.example" ".env" >nul

echo.
echo ============================================================
echo  Готово! Запуск Джарвиса: run.bat
echo.
echo  Для локальной модели установите Ollama: https://ollama.com/download
echo  и выполните в терминале:  ollama pull qwen3:8b
echo  Ключи GigaChat и Gemini впишите в файл .env
echo ============================================================
pause
exit /b 0

:no_python
echo Python не найден. Установите Python 3.10 или новее: https://www.python.org/downloads/
echo При установке отметьте галочку "Add python.exe to PATH".
pause
exit /b 1

:old_python
echo Нужен Python 3.10 или новее. Установите свежую версию: https://www.python.org/downloads/
pause
exit /b 1

:error
echo.
echo Ошибка установки. Проверьте подключение к интернету и запустите install.bat ещё раз.
pause
exit /b 1

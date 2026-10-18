@echo off
chcp 65001 > nul
title Запуск Discord Бота

echo ===================================
echo   Запуск Discord Бота...
echo ===================================

:: Проверка виртуального окружения (venv)
if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
)

:: Проверка зависимостей
echo Проверка зависимостей...
pip install -r requirements.txt --quiet

:: Запуск бота
echo Запуск main.py...
python main.py

pause
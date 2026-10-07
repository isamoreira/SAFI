@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
call .venv\Scripts\activate.bat
:loop
echo [%date% %time%] Iniciando o bot...
python bot.py
echo [%date% %time%] O bot parou. Reiniciando em 10 segundos... (feche esta janela para encerrar)
timeout /t 10 /nobreak >nul
goto loop
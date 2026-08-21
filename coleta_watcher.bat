@echo off
REM Vigia da coleta manual do painel /gestao.
REM Registrar no Agendador do Windows pra rodar a cada ~3 minutos.
cd /d "%~dp0"
if not exist logs mkdir logs
".venv\Scripts\python.exe" coleta_watcher.py >> "logs\coleta_watcher.log" 2>&1

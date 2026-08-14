@echo off
REM Coleta o Super Pague Menos (Convertiez) — mais lento (abre cada produto), 1x/dia.
cd /d "%~dp0"
".venv\Scripts\python.exe" coletar.py superpaguemenos
".venv\Scripts\python.exe" notificar_alertas.py

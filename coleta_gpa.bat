@echo off
REM Coleta o Pao de Acucar (GPA) — busca por termo + 1 detalhe por produto (EAN),
REM entao e mais lento (~1-2h). 1x/dia, de madrugada. Cap de rodada em params
REM (limite_por_rodada) evita rodar demais. Historico so grava mudanca (dedup).
cd /d "%~dp0"
".venv\Scripts\python.exe" coletar.py paodeacucar
".venv\Scripts\python.exe" notificar_alertas.py

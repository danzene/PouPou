@echo off
REM Refresh diario dos itens JA CONHECIDOS do Super Pague Menos (Convertiez).
REM Re-busca cada URL conhecida e atualiza o preco -> mantem o SPM fresco no DIA
REM (a varredura por sitemap e rotativa e demora dias por item). Uma requisicao por
REM item, entao roda devagar (~20 min) -> ideal de madrugada. Historico so grava
REM mudanca de preco (dedup), entao nao incha o banco.
cd /d "%~dp0"
".venv\Scripts\python.exe" refrescar_convertiez.py
".venv\Scripts\python.exe" notificar_alertas.py

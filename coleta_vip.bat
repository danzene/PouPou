@echo off
REM Coleta dos mercados VIPCommerce (Enxuto/Dalben/Taquaral/Spani) + Tenda.
REM Eles NAO tem refresh leve (como os VTEX tem no refrescar_precos.bat), entao a unica
REM forma de manter o preco fresco e re-coletar. Roda em TAREFA PROPRIA varias vezes ao dia
REM (MercadoBarato-VipTenda, a cada 8h) pra nao ficarem 1 dia atrasados no fim da fila do
REM crawl VTEX. Historico so grava mudanca de preco (dedup no inserir_preco).
cd /d "%~dp0"
".venv\Scripts\python.exe" coletar.py enxuto dalben taquaral tenda spani
".venv\Scripts\python.exe" notificar_alertas.py

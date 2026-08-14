@echo off
REM Refresh RAPIDO e LEVE de precos dos mercados VTEX (reconsulta em lote os EANs ja
REM conhecidos via fq=alternateIds_Ean, ~40 por requisicao, poucos minutos). Poucas
REM requisicoes -> baixo risco de bloqueio, pode rodar com frequencia.
REM Agendado a cada 3h pra manter o preco sempre fresco entre as varreduras completas
REM diarias (coleta_diaria.bat). O historico so cresce quando o preco MUDA (dedup no
REM inserir_preco), entao rodar 3/3h nao incha o banco.
cd /d "%~dp0"
".venv\Scripts\python.exe" refrescar.py
".venv\Scripts\python.exe" notificar_alertas.py

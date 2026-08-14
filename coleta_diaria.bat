@echo off
REM Job diário alternativo: coleta TODOS os mercados de uma vez + notifica alertas +
REM manutencao (poda 90 dias). O agendamento padrao NAO usa este arquivo (usa
REM coleta_rapidos.bat + coleta_convertiez.bat separados); ele fica aqui como opcao
REM de "rodar tudo manualmente" num comando so.
cd /d "%~dp0"
".venv\Scripts\python.exe" coletar.py
".venv\Scripts\python.exe" notificar_alertas.py
".venv\Scripts\python.exe" manutencao.py

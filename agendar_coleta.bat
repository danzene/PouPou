@echo off
REM ============================================================================
REM Registra as tarefas do MercadoBarato no Agendador de Tarefas do Windows.
REM Rode UMA vez (duplo clique). Para mudar horarios, edite e rode de novo (/f
REM sobrescreve). Para remover, use os schtasks /delete no fim deste arquivo.
REM
REM  1) MercadoBarato-Convertiez  -> Super Pague Menos (lento), 1x/dia 07:00
REM  2) MercadoBarato-Rapidos     -> crawl completo dos mercados rapidos + poda,
REM                                  1x/dia 08:00. Este e o crawl PESADO do dia ->
REM                                  1x/dia pra nao arriscar bloqueio nos sites.
REM  3) MercadoBarato-RefreshRapido -> refresh LEVE dos VTEX a cada 3h (lote de
REM                                  EANs, poucas requisicoes -> nao bloqueia).
REM  O historico so cresce quando o preco MUDA (dedup no inserir_preco), entao
REM  rodar o refresh de 3/3h mantem tudo fresco sem inchar o banco.
REM ============================================================================

REM tarefa antiga consolidada (se existir) -> remove pra nao duplicar
schtasks /delete /tn "MercadoBarato-ColetaDiaria" /f >nul 2>&1

schtasks /create /tn "MercadoBarato-Convertiez"    /tr "\"%~dp0coleta_convertiez.bat\"" /sc daily  /st 07:00 /f
schtasks /create /tn "MercadoBarato-Rapidos"       /tr "\"%~dp0coleta_rapidos.bat\""    /sc daily  /st 08:00 /f
schtasks /create /tn "MercadoBarato-RefreshRapido" /tr "\"%~dp0refrescar_precos.bat\""  /sc hourly /mo 3 /st 11:00 /f

echo.
echo Tarefas registradas:
echo   MercadoBarato-Convertiez    - Super Pague Menos 1x/dia 07:00
echo   MercadoBarato-Rapidos       - mercados rapidos + poda 1x/dia 08:00
echo   MercadoBarato-RefreshRapido - refresh VTEX a cada 3h
echo.
echo Para REMOVER depois:
echo   schtasks /delete /tn "MercadoBarato-Convertiez" /f
echo   schtasks /delete /tn "MercadoBarato-Rapidos" /f
echo   schtasks /delete /tn "MercadoBarato-RefreshRapido" /f
pause

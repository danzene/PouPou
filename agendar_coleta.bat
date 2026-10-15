@echo off
REM ============================================================================
REM PLANO B. A coleta hoje roda NA NUVEM (GitHub Actions, .github/workflows/coleta-*.yml
REM e refresh-vtex.yml), sem depender de PC ligado. Use este script so se quiser voltar
REM a coletar pelo computador (ex.: GitHub fora do ar) - e desligue os workflows antes,
REM pra nao coletar em dobro.
REM
REM Registra as tarefas do MercadoBarato no Agendador de Tarefas do Windows.
REM Rode UMA vez (duplo clique) num PC novo, DEPOIS de: iniciar.bat (cria o .venv)
REM e .env com DATABASE_URL. Para mudar horarios, edite e rode de novo (/f
REM sobrescreve). Para remover, use os schtasks /delete no fim deste arquivo.
REM
REM  1) MercadoBarato-Rapidos       -> crawl completo dos VTEX + poda + conferidor,
REM                                    1x/dia 08:00 (crawl PESADO -> 1x/dia).
REM  2) MercadoBarato-RefreshRapido -> refresh LEVE dos VTEX a cada 3h.
REM  3) MercadoBarato-VipTenda      -> VIP Commerce + Tenda a cada 8h.
REM  4) MercadoBarato-GPA           -> Pao de Acucar 1x/dia 03:00 (lento, ~1-2h).
REM  5) MercadoBarato-Watcher       -> vigia da coleta manual do /gestao, a cada 3 min.
REM  O Super Pague Menos NAO roda aqui: ele e coletado na nuvem pelo GitHub Actions
REM  (.github/workflows/paguemenos-nuvem.yml), que nao depende deste PC.
REM
REM  Tudo roda OCULTO via pythonw + run_bat_hidden.py (log em logs\<nome>.log).
REM  Obs.: as tarefas so rodam com o PC ligado e voce logado.
REM ============================================================================
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
  echo ERRO: .venv nao encontrado. Rode o iniciar.bat primeiro.
  pause
  exit /b 1
)

set "PYW=\"%~dp0.venv\Scripts\pythonw.exe\" \"%~dp0run_bat_hidden.py\""

REM tarefas antigas (se existirem) -> remove pra nao duplicar
schtasks /delete /tn "MercadoBarato-ColetaDiaria" /f >nul 2>&1
schtasks /delete /tn "MercadoBarato-Convertiez" /f >nul 2>&1

schtasks /create /tn "MercadoBarato-Rapidos"       /tr "%PYW% \"%~dp0coleta_rapidos.bat\""    /sc daily  /st 08:00 /f
schtasks /create /tn "MercadoBarato-RefreshRapido" /tr "%PYW% \"%~dp0refrescar_precos.bat\""  /sc hourly /mo 3 /st 11:00 /f
schtasks /create /tn "MercadoBarato-VipTenda"      /tr "%PYW% \"%~dp0coleta_vip.bat\""        /sc hourly /mo 8 /st 06:00 /f
schtasks /create /tn "MercadoBarato-GPA"           /tr "%PYW% \"%~dp0coleta_gpa.bat\""        /sc daily  /st 03:00 /f
REM Watcher vai direto no .py: o coleta_watcher.bat redireciona pro mesmo logs\coleta_watcher.log
REM que o run_bat_hidden abre -> "arquivo ja esta sendo usado" e exit 1 a cada rodada.
schtasks /create /tn "MercadoBarato-Watcher"       /tr "\"%~dp0.venv\Scripts\pythonw.exe\" \"%~dp0coleta_watcher.py\"" /sc minute /mo 3 /f

echo.
echo Tarefas registradas:
echo   MercadoBarato-Rapidos       - VTEX completo + poda 1x/dia 08:00
echo   MercadoBarato-RefreshRapido - refresh VTEX a cada 3h
echo   MercadoBarato-VipTenda      - VIP Commerce + Tenda a cada 8h
echo   MercadoBarato-GPA           - Pao de Acucar 1x/dia 03:00
echo   MercadoBarato-Watcher       - vigia do /gestao a cada 3 min
echo.
echo Para REMOVER depois:
echo   schtasks /delete /tn "MercadoBarato-Rapidos" /f
echo   schtasks /delete /tn "MercadoBarato-RefreshRapido" /f
echo   schtasks /delete /tn "MercadoBarato-VipTenda" /f
echo   schtasks /delete /tn "MercadoBarato-GPA" /f
echo   schtasks /delete /tn "MercadoBarato-Watcher" /f
pause

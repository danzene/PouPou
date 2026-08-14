@echo off
title Comparador de Precos - RM Campinas
cd /d "%~dp0"

REM --- primeira execucao: cria o ambiente e instala as dependencias ---
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo  Primeira execucao - preparando o ambiente. Aguarde...
  python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

REM libera a porta 8080 se ja houver um servidor rodando nela
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }" >nul 2>&1

echo.
echo  ============================================================
echo    Comparador de Precos - RM Campinas
echo.
echo    Abrindo no navegador: http://localhost:8080
echo    Deixe esta janela ABERTA enquanto usa o app.
echo    Para PARAR o app: feche esta janela.
echo  ============================================================
echo.

REM abre o navegador (aguarda uns segundos o servidor subir)
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 4; Start-Process 'http://localhost:8080'"

REM sobe o servidor (esta linha segura a janela aberta)
".venv\Scripts\python.exe" -m uvicorn api.main:app --host 127.0.0.1 --port 8080

echo.
echo  Servidor encerrado. Pode fechar esta janela.
pause

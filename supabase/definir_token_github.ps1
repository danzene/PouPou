# Guarda o token do GitHub (que dispara as coletas) no Vault do Supabase.
# O token e digitado aqui (nao aparece na tela), testado na API do GitHub e gravado
# como 'github_dispatch_token' - nunca vai pra arquivo, log ou chat.
# Uso: powershell -ExecutionPolicy Bypass -File supabase\definir_token_github.ps1
$ErrorActionPreference = 'Stop'
$proj = Split-Path -Parent $PSScriptRoot
Write-Host ''
Write-Host 'Cole o token do GitHub (github_pat_...) e aperte Enter. Ele nao aparece enquanto cola:' -ForegroundColor Cyan
$sec = Read-Host -AsSecureString 'Token'
$tok = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)).Trim()
if (-not $tok) { Write-Host 'Token vazio. Rode de novo.' -ForegroundColor Red; exit 1 }

# 1) testa no GitHub: precisa enxergar os workflows do PouPou
try {
    $h = @{ Authorization = "Bearer $tok"; Accept = 'application/vnd.github+json'; 'User-Agent' = 'poupou' }
    $r = Invoke-RestMethod -Uri 'https://api.github.com/repos/danzene/PouPou/actions/workflows' -Headers $h
    Write-Host "Token OK no GitHub ($($r.total_count) workflows visiveis)." -ForegroundColor Green
} catch {
    Write-Host ">>> O GitHub recusou o token: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host '    Confira: repositorio danzene/PouPou e permissao Actions = Read and write.' -ForegroundColor Red
    exit 1
}

# 2) grava no Vault (cria ou atualiza), passando o token por variavel de ambiente
$env:POUPOU_GH_TOKEN = $tok
Push-Location $proj
# (o codigo vai num .py temporario: o PowerShell 5.1 come as aspas de 'python -c')
$py = Join-Path $env:TEMP 'poupou_vault.py'
@'
import os, psycopg
from dotenv import load_dotenv
load_dotenv(".env", override=True)
tok = os.environ.pop("POUPOU_GH_TOKEN")
with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
    row = c.execute("select id from vault.secrets where name = 'github_dispatch_token'").fetchone()
    if row:
        c.execute("select vault.update_secret(%s, %s)", (row[0], tok))
    else:
        c.execute("select vault.create_secret(%s, 'github_dispatch_token', 'dispara as coletas do PouPou')", (tok,))
print("GRAVADO NO VAULT")
'@ | Set-Content -Path $py -Encoding UTF8
& .\.venv\Scripts\python.exe $py
$ok = $LASTEXITCODE
Remove-Item $py -ErrorAction SilentlyContinue
Remove-Item Env:POUPOU_GH_TOKEN -ErrorAction SilentlyContinue
Pop-Location
if ($ok -eq 0) { Write-Host '>>> DEU CERTO! Pode voltar pro chat.' -ForegroundColor Green } else { Write-Host '>>> Falhou ao gravar no Supabase.' -ForegroundColor Red }

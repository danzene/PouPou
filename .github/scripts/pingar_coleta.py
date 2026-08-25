"""Despertador da coleta do Pague Menos.

Roda nos servidores do GitHub (EUA). NAO fala com o Pague Menos (que bloqueia IP de fora do
BR) — so bate no nosso proprio Vercel (Sao Paulo, IP brasileiro), que faz a coleta de verdade,
uma leva pequena por chamada. O token de autenticacao e lido do proprio banco (tabela
app_config), usando o DATABASE_URL que ja e secret do repo — entao nao precisa de segredo novo.
O token fica so na memoria deste processo (nunca vai pra log nem pra env)."""
import os
import sys
import time

import httpx
import psycopg

dsn = os.environ.get("DATABASE_URL", "").strip()
base = os.environ.get("BASE_URL", "https://poupou-three.vercel.app").rstrip("/")
batches = int(os.environ.get("BATCHES", "50"))
if not dsn:
    sys.exit("DATABASE_URL ausente")

with psycopg.connect(dsn, connect_timeout=20) as c:
    row = c.execute("SELECT valor FROM app_config WHERE chave = 'coleta_token'").fetchone()
token = row[0] if row else None
if not token:
    sys.exit("token de coleta nao encontrado no banco (app_config.coleta_token)")

H = {"X-Coleta-Token": token}
total = 0
with httpx.Client(timeout=95.0) as cli:
    for i in range(1, batches + 1):
        try:
            r = cli.get(f"{base}/cron/pm-batch", headers=H)
        except Exception as e:  # noqa: BLE001
            print(f"leva {i}: erro {type(e).__name__}: {e}")
            if i == 1:
                sys.exit(1)
            break
        if r.status_code != 200:
            print(f"leva {i}: HTTP {r.status_code} {r.text[:200]}")
            if i == 1:
                sys.exit("primeira leva falhou (token/Vercel). Abortando.")
            break
        d = r.json()
        total += int(d.get("feitos", 0) or 0)
        print(f"leva {i}: feitos={d.get('feitos')} cursor={d.get('de')}->{d.get('para')} "
              f"de {d.get('total')} bloqueado={d.get('bloqueado')}")
        if d.get("bloqueado"):
            print("Vercel sinalizou bloqueio -> parando cedo")
            break
        if d.get("total") == 0:
            print("sem URLs conhecidas -> parando")
            break
        time.sleep(2)

print(f"==> TOTAL de produtos re-checados nesta rodada: {total}")

# descobrir produtos NOVOS (ate 3 levas: le o sitemap e coleta os que faltam; para cedo)
novos_total = 0
for j in range(1, 4):
    try:
        r = httpx.get(f"{base}/cron/pm-descobrir", headers=H, timeout=95.0)
    except Exception as e:  # noqa: BLE001
        print(f"descobrir {j}: erro {type(e).__name__}: {e}")
        break
    if r.status_code != 200:
        print(f"descobrir {j}: HTTP {r.status_code} {r.text[:150]}")
        break
    d = r.json()
    nc = int(d.get("novos_coletados", 0) or 0)
    novos_total += nc
    print(f"descobrir {j}: novos_coletados={nc} novos_no_sitemap={d.get('novos_no_sitemap')} "
          f"sitemap={d.get('sitemap_total')}")
    if nc == 0 or d.get("bloqueado"):
        break
    time.sleep(2)
print(f"==> TOTAL de produtos NOVOS descobertos nesta rodada: {novos_total}")

try:
    r = httpx.get(f"{base}/cron/pm-finalizar", headers=H, timeout=90.0)
    print(f"finalizar (resumo): HTTP {r.status_code} {r.text[:120]}")
except Exception as e:  # noqa: BLE001
    print(f"finalizar erro: {type(e).__name__}: {e}")

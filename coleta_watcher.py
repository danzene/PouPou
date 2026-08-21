"""Vigia da COLETA MANUAL do painel /gestao.

O admin clica no botão -> vira um pedido 'pendente' na tabela coleta_jobs (no Supabase).
Este script (rodando no PC do dono, pelo Agendador do Windows a cada ~3 min) pega o
pendente e roda o coletar.py (todos os mercados), atualizando o status.

Registrar no Agendador via coleta_watcher.bat. Não roda duas coletas ao mesmo tempo.
"""
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(RAIZ / ".env")
from db import database  # noqa: E402

MAX_MIN = 90  # se um job ficar 'rodando' mais que isso, considera travado


def agora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(ts):
    if not ts:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


# 1) pega um pedido pendente (e trava concorrência) ------------------------------
conn = database.conectar()
jid = None
try:
    rod = conn.execute(
        "SELECT id, iniciado_em FROM coleta_jobs WHERE status = 'rodando' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if rod:
        ini = _parse(rod["iniciado_em"])
        travado = ini is not None and (datetime.now(timezone.utc) - ini) > timedelta(minutes=MAX_MIN)
        if not travado:
            print("já tem coleta rodando — saindo.")
            sys.exit(0)
        conn.execute("UPDATE coleta_jobs SET status = 'erro', terminado_em = ?, "
                     "resultado = 'job travado (watcher reiniciou)' WHERE id = ?", (agora(), rod["id"]))
        print(f"job {rod['id']} estava travado -> marcado erro; seguindo.")

    job = conn.execute(
        "SELECT id FROM coleta_jobs WHERE status = 'pendente' ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if not job:
        print("sem pedidos pendentes.")
        sys.exit(0)
    jid = job["id"]
    conn.execute("UPDATE coleta_jobs SET status = 'rodando', iniciado_em = ? WHERE id = ?", (agora(), jid))
    print(f"job {jid}: iniciando coletar.py (todos os mercados)…")
finally:
    conn.close()

# 2) roda a coleta completa (pode demorar; fora da conexão) ----------------------
ok, resumo = False, ""
try:
    r = subprocess.run([sys.executable, str(RAIZ / "coletar.py")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=60 * 60)  # teto de 1h
    ok = (r.returncode == 0)
    resumo = "saída:\n" + (r.stdout or "")[-700:]
    if r.stderr:
        resumo += "\nerros:\n" + r.stderr[-700:]
except Exception as e:  # noqa: BLE001
    resumo = f"exceção no watcher ao rodar coletar.py: {e}"

# 3) grava o resultado -----------------------------------------------------------
conn = database.conectar()
try:
    conn.execute("UPDATE coleta_jobs SET status = ?, terminado_em = ?, resultado = ? WHERE id = ?",
                 ("concluido" if ok else "erro", agora(), resumo[-1800:], jid))
    print("coleta concluída." if ok else "coleta terminou com erro.")
finally:
    conn.close()

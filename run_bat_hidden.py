"""Roda um .bat SEM abrir janela e grava a saida em logs/<nome>.log.

Chamado pelas TAREFAS AGENDADAS via pythonw.exe (que nao abre janela) — abordagem
confiavel (o coleta_watcher tambem usa pythonw e funciona), no lugar do VBS que dava
erro no Agendador. A janela do cmd/coletores fica oculta via CREATE_NO_WINDOW.

Uso:  pythonw.exe run_bat_hidden.py "C:\\...\\algum.bat"
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
CREATE_NO_WINDOW = 0x08000000

if len(sys.argv) < 2:
    sys.exit("uso: run_bat_hidden.py <caminho.bat>")
bat = sys.argv[1]
logdir = RAIZ / "logs"
logdir.mkdir(exist_ok=True)
logf = logdir / (Path(bat).stem + ".log")

# Rotação simples: o log é 'append' entre execuções e podia crescer sem limite (o refresh
# do Convertiez loga milhares de requests -> chegou a 226 MB, risco de encher o disco e
# derrubar a coleta). Se passar de LOG_MAX, guarda o ciclo anterior como .1 e recomeça.
LOG_MAX = 15 * 1024 * 1024
try:
    if logf.exists() and logf.stat().st_size > LOG_MAX:
        bak = logf.with_name(logf.name + ".1")
        if bak.exists():
            bak.unlink()
        logf.rename(bak)
except OSError:
    pass

with open(logf, "a", encoding="utf-8", errors="replace") as f:
    f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} iniciando {Path(bat).name} =====\n")
    f.flush()
    try:
        r = subprocess.run(["cmd", "/c", bat], stdout=f, stderr=subprocess.STDOUT,
                           creationflags=CREATE_NO_WINDOW, cwd=str(RAIZ))
        f.write(f"===== {datetime.now():%H:%M:%S} fim (exit {r.returncode}) =====\n")
    except Exception as e:  # noqa: BLE001
        f.write(f"===== ERRO no run_bat_hidden: {e} =====\n")

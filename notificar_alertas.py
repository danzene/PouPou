"""Verifica a lista e dispara notificações (toast do Windows) dos alertas.

Rodado logo após a coleta (ver coleta_diaria.bat). Um alerta dispara quando o
MENOR preço atual de um item da lista ficou ESTRITAMENTE abaixo do preço-alvo.

Uso: python notificar_alertas.py
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

from dotenv import load_dotenv


def alertas_disparados(conn: sqlite3.Connection) -> list[dict]:
    linhas = conn.execute(
        """
        SELECT l.ean, l.preco_alvo, pr.nome_canonico AS nome,
               (SELECT MIN(x.preco) FROM precos x
                JOIN (SELECT mercado_id, MAX(coletado_em) mx FROM precos WHERE ean = l.ean GROUP BY mercado_id) u
                  ON u.mercado_id = x.mercado_id AND u.mx = x.coletado_em
                WHERE x.ean = l.ean) AS menor
        FROM lista l JOIN produtos pr ON pr.ean = l.ean
        WHERE l.preco_alvo IS NOT NULL
        """
    ).fetchall()
    out = []
    for r in linhas:
        if r["menor"] is not None and r["menor"] < r["preco_alvo"]:
            out.append({"nome": r["nome"], "menor": r["menor"], "alvo": r["preco_alvo"]})
    return out


def toast(titulo: str, mensagem: str) -> None:
    """Mostra um balão de notificação do Windows (NotifyIcon), sem dependências extras."""
    ps = f"""
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.BalloonTipTitle = @'
{titulo}
'@
$n.BalloonTipText = @'
{mensagem}
'@
$n.Visible = $true
$n.ShowBalloonTip(15000)
Start-Sleep -Seconds 8
$n.Dispose()
"""
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       timeout=30, capture_output=True)
    except Exception as exc:  # noqa: BLE001
        print(f"(toast falhou: {exc})")


def main() -> int:
    load_dotenv()
    db = os.getenv("DB_PATH", "dados/precos.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    alertas = alertas_disparados(conn)
    conn.close()

    if not alertas:
        print("Sem alertas de preço.")
        return 0

    print(f"{len(alertas)} alerta(s):")
    linhas = []
    for a in alertas:
        linha = f"• {a['nome'][:40]}: R$ {a['menor']:.2f} (alvo R$ {a['alvo']:.2f})"
        print(linha)
        linhas.append(linha)

    titulo = f"🔔 {len(alertas)} preço(s) no alvo — Comparador RM Campinas"
    toast(titulo, "\n".join(linhas[:6]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

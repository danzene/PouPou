"""Recalcula unidade/qtd_embalagem (produtos) e preco_por_unidade (precos) a partir
dos nomes já salvos — SEM tocar na rede. Rode depois de melhorar as regras de
`normalizar.py` para atualizar o banco existente.

Uso: python -m analise.renormalizar
"""
from __future__ import annotations

import os
import sqlite3

from dotenv import load_dotenv

from analise.normalizar import extrair_medida, preco_por_unidade


def main() -> int:
    load_dotenv()
    conn = sqlite3.connect(os.getenv("DB_PATH", "dados/precos.db"))
    conn.row_factory = sqlite3.Row

    produtos = conn.execute("SELECT ean, nome_canonico FROM produtos").fetchall()
    n_prod = n_prec = 0
    for prod in produtos:
        medida = extrair_medida(prod["nome_canonico"])
        conn.execute(
            "UPDATE produtos SET unidade = ?, qtd_embalagem = ? WHERE ean = ?",
            (medida.unidade if medida else None,
             medida.qtd_base if medida else None,
             prod["ean"]),
        )
        n_prod += 1
        for pr in conn.execute("SELECT id, preco FROM precos WHERE ean = ?", (prod["ean"],)):
            ppu, _ = preco_por_unidade(pr["preco"], medida)
            conn.execute("UPDATE precos SET preco_por_unidade = ? WHERE id = ?", (ppu, pr["id"]))
            n_prec += 1

    conn.commit()
    comp = conn.execute("SELECT COUNT(*) FROM precos WHERE preco_por_unidade IS NOT NULL").fetchone()[0]
    tot = conn.execute("SELECT COUNT(*) FROM precos").fetchone()[0]
    print(f"Renormalizado: {n_prod} produtos, {n_prec} snapshots.")
    print(f"Cobertura R$/un: {comp}/{tot} = {100*comp/tot:.1f}%")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

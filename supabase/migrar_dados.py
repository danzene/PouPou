"""Migra os dados de PREÇO do SQLite local -> Postgres do Supabase (uma vez só).

Pré-requisito: DATABASE_URL setada no .env
  (Supabase → Project Settings → Database → Connection string → URI).

Uso:
  python supabase/migrar_dados.py                 # migra tudo (inclui histórico 'precos')
  python supabase/migrar_dados.py --sem-historico # pula 'precos' (histórico) — bem mais rápido

- NÃO toca nas tabelas de usuário (profiles/lista/favoritos).
- Limpa as tabelas de PREÇO no Postgres e recarrega do zero (pode rodar de novo sem duplicar).
- Preserva os ids dos mercados (são referenciados por mercado_id).
"""
from __future__ import annotations

import os
import sys
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")

# Windows: console cp1252 quebra em símbolos (✓). Força UTF-8.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

DSN = os.getenv("DATABASE_URL", "").strip()
SQLITE_PATH = os.getenv("DB_PATH", "dados/precos.db")

# Ordem respeita as FKs (pais antes dos filhos). 'precos.id' é gerado no Postgres (não migra).
PLANO = [
    ("categorias",   ["slug", "nome", "disponivel", "ordem"]),
    ("mercados",     ["id", "slug", "nome", "url_base", "plataforma", "logo_url",
                      "cor_badge", "cidade", "loja_ref", "ativo"]),
    ("produtos",     ["ean", "nome_canonico", "marca", "categoria", "unidade",
                      "qtd_embalagem", "imagem_url", "n_mercados", "menor_preco"]),
    ("precos_atual", ["ean", "mercado_id", "preco", "preco_por_unidade", "origem",
                      "coletado_em", "so_no_site", "preco_de", "desconto_pct", "oferta_tipo"]),
    ("pereciveis",   ["mercado_id", "cod_interno", "nome", "nome_norm", "categoria", "preco",
                      "unidade", "imagem_url", "url", "coletado_em",
                      "so_no_site", "preco_de", "desconto_pct", "oferta_tipo"]),
    ("links",        ["ean", "mercado_id", "url", "atualizado_em"]),
    ("precos",       ["ean", "mercado_id", "preco", "preco_por_unidade", "origem", "coletado_em"]),
]


def _cols_sqlite(scon, tbl):
    return {r[1] for r in scon.execute(f"PRAGMA table_info({tbl})").fetchall()}


def main(argv):
    if not DSN:
        sys.exit("Defina DATABASE_URL no .env (Supabase → Settings → Database → Connection string).")
    import psycopg  # só precisa aqui

    sem_hist = "--sem-historico" in argv
    spath = SQLITE_PATH if os.path.isabs(SQLITE_PATH) else str(RAIZ / SQLITE_PATH)
    scon = sqlite3.connect(spath)
    scon.row_factory = sqlite3.Row
    try:
        pg = psycopg.connect(DSN, autocommit=False)
    except Exception:  # noqa: BLE001 — não vaza a DSN (tem senha) no traceback
        scon.close()
        sys.exit("Não conectei ao Postgres. Confira a DATABASE_URL no .env "
                 "(host, porta, senha) e a conexão. (erro de conexão ocultado por segurança)")
    try:
        with pg.cursor() as cur:
            print("Limpando tabelas de PREÇO no Postgres (usuário fica intacto)…")
            cur.execute("TRUNCATE categorias, mercados, produtos, precos_atual, "
                        "pereciveis, links, precos RESTART IDENTITY CASCADE")
        pg.commit()

        for tbl, cols in PLANO:
            if tbl == "precos" and sem_hist:
                print("  precos (histórico): PULADO (--sem-historico)")
                continue
            disp = [c for c in cols if c in _cols_sqlite(scon, tbl)]
            collist = ", ".join(disp)
            total = scon.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            print(f"  {tbl}: {total} linhas… ", end="", flush=True)
            n = 0

            if tbl == "mercados":
                # poucos registros + precisa preservar o id (generated always) -> OVERRIDING SYSTEM VALUE
                ph = ", ".join(["%s"] * len(disp))
                with pg.cursor() as cur:
                    for row in scon.execute(f"SELECT {collist} FROM {tbl}"):
                        cur.execute(
                            f"INSERT INTO mercados ({collist}) OVERRIDING SYSTEM VALUE VALUES ({ph})",
                            tuple(row[c] for c in disp))
                        n += 1
                    cur.execute("SELECT setval(pg_get_serial_sequence('mercados','id'), "
                                "COALESCE((SELECT MAX(id) FROM mercados), 1))")
            else:
                # bulk via COPY (rápido). precos vai SEM id -> o Postgres gera.
                with pg.cursor() as cur, cur.copy(f"COPY {tbl} ({collist}) FROM STDIN") as cp:
                    for row in scon.execute(f"SELECT {collist} FROM {tbl}"):
                        cp.write_row(tuple(row[c] for c in disp))
                        n += 1
            pg.commit()
            print(f"{n} ✓")

        # confere as contagens no Postgres
        print("\nContagens no Postgres:")
        with pg.cursor() as cur:
            for tbl, _ in PLANO:
                cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                print(f"  {tbl:14} {cur.fetchone()[0]}")
        print("\nMigração concluída ✓")
    except Exception:
        pg.rollback()
        raise
    finally:
        scon.close()
        pg.close()


if __name__ == "__main__":
    main(sys.argv)

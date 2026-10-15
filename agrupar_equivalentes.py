"""Junta o MESMO produto que aparece com códigos de barras diferentes em mercados diferentes.

Fabricante troca o EAN quando muda a embalagem e alguns mercados seguem com o código
antigo (ex.: Óleo de Soja Liza 900ml: 7896036090336 só no Covabra x 7896036090244 em 7
mercados). Sem juntar, o Covabra nunca entra na comparação desses itens.

Regra CONSERVADORA (só junta quando tudo bate):
  1. mesma marca;
  2. mesmo tamanho (normalizar.extrair_medida: 900ml = 0,9 L);
  3. mesmas palavras no nome, fora embalagem/ruído ('garrafa', 'pacote', 'tipo 1'...).
     'com'/'sem' contam ('com sal' != 'sem sal');
  4. nenhum mercado vende os dois códigos (se vende, são produtos diferentes);
  5. mesma quantidade no pacote ('56g' != '56g 36 unidades') e nada de 'leve X pague Y'.
O código com mais mercados vira o principal. Grava em `produto_equivalente`
(ean -> ean_principal); a view `precos_atual_unif` (database.garantir_equivalentes)
junta os preços. Os dados de cada código ficam intactos: é só uma tabela de/para,
recalculada do zero a cada rodada.

Uso:
    python agrupar_equivalentes.py            # recalcula e grava
    python agrupar_equivalentes.py --simular  # só mostra, não grava
"""
from __future__ import annotations

import logging
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

from analise import normalizar
from analise.ranking import _unaccent
from db import database

RAIZ = Path(__file__).resolve().parent
load_dotenv(RAIZ / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("agrupar_equivalentes")

# palavras de embalagem/ruído que não mudam o produto ('com'/'sem' NÃO estão aqui)
# 'lata', 'sache' e 'refil' NÃO são ruído: Ninho lata x sachê, sabonete refil x válvula são
# itens diferentes. Letra sozinha e número solto contam (fralda M x G, filtro 102 x 103).
_RUIDO = set("""garrafa pet latinha pacote pct caixa cx tp tetra pak frasco pote
vidro tipo 1 un und unidade unidades tampa embalagem economica tradicional original
de da do e em para p c s a o the""".split())
_RE_MEDIDA_TXT = re.compile(r"\d+[.,]?\d*\s*(kg|g|gr|mg|ml|l|lt|litros?|un|und)\b")
_RE_LEVE_PAGUE = re.compile(r"\bleve\b.*\bpague\b")
# quantas unidades no pacote: '36 unidades', 'c/ 12', 'com 6', '6x', 'x 6', 'pack 12'
_RE_QTD = re.compile(r"(\d+)\s*(?:unidades?|unid|und|un)\b|\bc/\s*(\d+)|\bcom\s+(\d+)\b|"
                     r"\b(\d+)\s*x\b|\bx\s*(\d+)\b|\bpack\s*(\d+)")


def _qtd_pacote(nome_norm: str) -> int:
    """'Hamburguer Misto 56g 36 Unidades' -> 36; sem indicação -> 1."""
    m = _RE_QTD.search(nome_norm)
    if not m:
        return 1
    return int(next(g for g in m.groups() if g)) or 1


def _palavras(nome: str, marca: str) -> frozenset:
    n = _RE_MEDIDA_TXT.sub(" ", _unaccent(nome) or "")
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    mw = set(marca.split())
    n = _RE_QTD.sub(" ", n)
    return frozenset(w for w in n.split()
                     if w not in _RUIDO and w not in mw)


def calcular(conn) -> dict[str, str]:
    """{ean_alias: ean_principal} pela regra conservadora."""
    rows = conn.execute(
        "SELECT pr.ean, pr.nome_canonico, pr.marca, pa.mercado_id "
        "FROM produtos pr JOIN precos_atual pa ON pa.ean = pr.ean").fetchall()
    prods: dict[str, dict] = {}
    for r in rows:
        p = prods.get(r["ean"])
        if p is None:
            p = prods[r["ean"]] = {"ean": r["ean"], "nome": r["nome_canonico"] or "",
                                   "marca": (_unaccent(r["marca"]) or "").strip(), "ms": set()}
        p["ms"].add(r["mercado_id"])

    blocos: dict[tuple, list[dict]] = defaultdict(list)
    for p in prods.values():
        med = normalizar.extrair_medida(p["nome"])
        if not p["marca"] or not med or _RE_LEVE_PAGUE.search(_unaccent(p["nome"]) or ""):
            continue
        pal = _palavras(p["nome"], p["marca"])
        if pal:
            blocos[(p["marca"], med.unidade, round(med.qtd_base, 3),
                    _qtd_pacote(_unaccent(p["nome"]) or ""), pal)].append(p)

    mapa: dict[str, str] = {}
    for grupo in blocos.values():
        if len(grupo) < 2:
            continue
        # mais mercados primeiro; um código só entra se não divide mercado com quem já entrou
        grupo.sort(key=lambda p: (-len(p["ms"]), p["ean"]))
        principal, usados = grupo[0], set(grupo[0]["ms"])
        for p in grupo[1:]:
            if p["ms"] & usados:
                continue
            usados |= p["ms"]
            mapa[p["ean"]] = principal["ean"]
    return mapa


def main(argv: list[str]) -> int:
    conn = database.conectar(os.getenv("DB_PATH", "dados/precos.db"))
    database.inicializar(conn)
    database.garantir_equivalentes(conn)
    mapa = calcular(conn)
    log.info("%d códigos juntados a %d produtos principais", len(mapa), len(set(mapa.values())))
    if "--simular" in argv:
        nomes = {r["ean"]: r["nome_canonico"] for r in conn.execute("SELECT ean, nome_canonico FROM produtos")}
        for a, p in list(mapa.items())[:40]:
            log.info("  %-45s -> %s", (nomes.get(a) or a)[:45], (nomes.get(p) or p)[:45])
        return 0
    conn.execute("DELETE FROM produto_equivalente")
    if mapa:
        conn.executemany("INSERT INTO produto_equivalente (ean, ean_principal) VALUES (?, ?)",
                         list(mapa.items()))
    conn.commit()
    conn.close()
    log.info("Gravado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

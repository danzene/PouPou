"""CONFERIDOR de preços — garante que o app NÃO fica diferente do site.

Depois de coletar, roda uma amostra: re-busca ao vivo no site (via os próprios
coletores, mesma lógica que alimenta o app) e compara com o que está GRAVADO no banco.
Divergência = preço defasado OU bug de lógica de coleta. Foca em CARNES/frescos (onde o
preço é crítico), mas cobre todos os mercados ativos.

Uso:
    python conferir_precos.py                    # todos os mercados ativos
    python conferir_precos.py enxuto savegnago   # só os informados
    python conferir_precos.py --tol 0.03         # tolerância 3% (padrão 2%)

Saída: por mercado, quantos conferidos, quantos OK e a lista de DIVERGÊNCIAS (banco x site).
Código de saída != 0 se houver divergência acima do limite — dá pra usar em alerta/CI.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from analise import normalizar
from coletar import _carregar_json, _instanciar
from coletores.base import ItemColeta, ItemPerecivel
from db import database

RAIZ = Path(__file__).resolve().parent
load_dotenv(RAIZ / ".env")

# Windows: o console/redirecionamento usa cp1252 e quebra nos símbolos (✓, ⚠). Força UTF-8.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("conferir")

# Termos de amostra: carnes em peso (o ponto crítico) + alguns itens comuns.
TERMOS = ["picanha", "contra file", "acem", "alcatra", "coxao", "fraldinha", "frango",
          "banana", "tomate", "cebola", "leite", "arroz", "cafe", "acucar", "oleo"]


def _chave(it) -> str:
    return it.ean if isinstance(it, ItemColeta) else f"f:{it.cod_interno}"


def _preco_gravado(conn, mid, it):
    """Preço atualmente GRAVADO no banco pra este item (o que o app mostra)."""
    if isinstance(it, ItemColeta):
        r = conn.execute("SELECT preco FROM precos_atual WHERE ean=? AND mercado_id=?",
                         (it.ean, mid)).fetchone()
    else:
        r = conn.execute("SELECT preco FROM pereciveis WHERE cod_interno=? AND mercado_id=?",
                         (it.cod_interno, mid)).fetchone()
    return r["preco"] if r else None


def _corrigir_item(conn, mid, it, agora):
    """Auto-cura: grava no banco o preço vivo recém-lido (mesma trilha da coleta).
    Reaproveita o dado que o conferidor JÁ buscou — não faz request extra ao site."""
    if isinstance(it, ItemPerecivel):
        database.upsert_perecivel(
            conn, mercado_id=mid, cod_interno=it.cod_interno, nome=it.nome,
            preco=it.preco, unidade=it.unidade, imagem_url=it.imagem_url, url=it.url,
            coletado_em=agora, so_no_site=it.so_no_site,
            preco_de=it.preco_de, desconto_pct=it.desconto_pct, oferta_tipo=it.oferta_tipo)
    else:
        medida = normalizar.extrair_medida(it.nome)
        ppu, _ = normalizar.preco_por_unidade(it.preco, medida)
        database.upsert_produto(
            conn, ean=it.ean, nome=it.nome, marca=it.marca, categoria=it.categoria,
            unidade=(medida.unidade if medida else it.unidade),
            qtd_embalagem=(medida.qtd_base if medida else None), imagem_url=it.imagem_url)
        database.inserir_preco(
            conn, ean=it.ean, mercado_id=mid, preco=it.preco, preco_por_unidade=ppu,
            coletado_em=agora, so_no_site=it.so_no_site,
            preco_de=it.preco_de, desconto_pct=it.desconto_pct, oferta_tipo=it.oferta_tipo)
        if it.url:
            database.upsert_link(conn, ean=it.ean, mercado_id=mid, url=it.url, atualizado_em=agora)


def conferir_mercado(conn, cfg, opts, tol, corrigir=False, agora=None):
    slug = cfg["slug"]
    mid_row = conn.execute("SELECT id FROM mercados WHERE slug=?", (slug,)).fetchone()
    if not mid_row:
        return None
    mid = mid_row["id"]
    try:
        col = _instanciar(cfg, **opts)
    except Exception as exc:  # noqa: BLE001
        log.warning("[%s] não instanciou: %s", slug, exc)
        return None
    if not hasattr(col, "coletar"):
        return None

    conferidos = ok = 0
    novos = corrigidos = 0
    divergencias = []
    vistos = set()
    try:
        for it in col.coletar([(t, None) for t in TERMOS]):
            if not isinstance(it, (ItemColeta, ItemPerecivel)):
                continue
            k = _chave(it)
            if k in vistos:
                continue
            vistos.add(k)
            gravado = _preco_gravado(conn, mid, it)
            if gravado is None:
                novos += 1  # item no site mas ainda não no banco (coleta ainda não pegou)
                if corrigir:  # grava o item que faltava (com o preço vivo)
                    _corrigir_item(conn, mid, it, agora)
                    corrigidos += 1
                continue
            conferidos += 1
            vivo = float(it.preco)
            base = max(vivo, gravado, 0.01)
            if abs(vivo - gravado) / base > tol:
                divergencias.append((it.nome, gravado, vivo, it.unidade))
                if corrigir:  # auto-cura: sincroniza o banco com o site AGORA
                    _corrigir_item(conn, mid, it, agora)
                    corrigidos += 1
            else:
                ok += 1
    except Exception as exc:  # noqa: BLE001
        log.warning("[%s] erro na conferência: %s", slug, exc)

    if corrigir and corrigidos:
        conn.commit()
    return {"slug": slug, "conferidos": conferidos, "ok": ok, "novos": novos,
            "divergencias": divergencias, "corrigidos": corrigidos}


def main(argv):
    tol = 0.02
    if "--tol" in argv:
        i = argv.index("--tol")
        try:
            tol = float(argv[i + 1]); del argv[i:i + 2]
        except (IndexError, ValueError):
            pass
    # --corrigir: além de apontar, SINCRONIZA o banco com o preço vivo na hora (auto-cura).
    corrigir = "--corrigir" in argv
    filtro = set(a for a in argv[1:] if not a.startswith("-"))

    delay = float(os.getenv("DELAY", "0.6"))
    timeout = float(os.getenv("TIMEOUT", "25"))
    contato = os.getenv("CONTATO", "contato@exemplo.com")
    db_path = os.getenv("DB_PATH", "dados/precos.db")
    mercados = _carregar_json(RAIZ / "config" / "mercados.json")["mercados"]
    alvos = [m for m in mercados if m.get("ativo") and m.get("classe")
             and (not filtro or m["slug"] in filtro)]

    conn = database.conectar(db_path)
    database.inicializar(conn)
    opts = dict(delay=delay, timeout=timeout, max_por_termo=8, contato=contato)
    agora = datetime.now(timezone.utc).isoformat()

    modo = " — MODO AUTO-CORREÇÃO (sincroniza o banco)" if corrigir else ""
    print(f"Conferindo {len(alvos)} mercado(s) — tolerância {tol*100:.0f}% — amostra por termo{modo}\n")
    total_div = total_corr = 0
    for cfg in alvos:
        r = conferir_mercado(conn, cfg, opts, tol, corrigir=corrigir, agora=agora)
        if r is None:
            print(f"  {cfg['slug']:16} — pulado")
            continue
        nd = len(r["divergencias"])
        total_div += nd
        total_corr += r["corrigidos"]
        marca = "✓ OK" if nd == 0 else f"⚠ {nd} DIVERGÊNCIA(S)"
        extra = f"  corrigidos={r['corrigidos']:3}" if corrigir else ""
        print(f"  {r['slug']:16} conferidos={r['conferidos']:3}  ok={r['ok']:3}  novos={r['novos']:3}{extra}  {marca}")
        for nome, grav, vivo, un in r["divergencias"][:12]:
            print(f"       BANCO R${grav:.2f} x SITE R${vivo:.2f} /{un or '-'}  {nome[:44]}")
    if corrigir and total_corr:
        database.rebuild_resumo(conn)  # atualiza menor_preco/n_mercados após as correções
        conn.commit()
    conn.close()
    print(f"\nTotal de divergências: {total_div}" + (f" | corrigidas no banco: {total_corr}" if corrigir else ""))
    # Com auto-correção, divergência não é erro (foi sanada); sem ela, sinaliza pra alerta.
    return 0 if corrigir else (1 if total_div else 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

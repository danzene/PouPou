"""Coletor Pão de Açúcar (GPA) — API pública `api.vendas.gpa.digital/pa` (sem auth).

Descoberto por engenharia reversa do front (Next.js): a busca é
    POST /pa/search/search  {terms, page, resultsPerPage, storeId, department:'ecom',
                             customerPlus, partner:'fallback', allowRedirect}
que devolve produtos com PREÇO (mas sem EAN). O EAN vem do detalhe do produto.

Detalhe: até 05/11/2026 era GET /pa/v4/products/ecom/{id} (JSON leve). Em 06/11 o GPA
tirou essa rota ("No matching handler", 404 em tudo) e o EAN passou a vir só da página do
produto, montada no servidor (Next.js). Hoje:
    1º) cache id->EAN dos links já gravados no banco (carregar_cache_ean) — a maioria;
    2º) GET /_next/data/{buildId}/produto/{id}.json -> pageProps.product.ean (só os novos);
        o buildId muda a cada deploy do site: lido do __NEXT_DATA__ da página do produto.

Como a busca é por TERMO (não há varredura por categoria acessível), este coletor usa
o modo termos-semente do projeto (coletar(sementes)): busca cada termo e, pra cada
produto, consulta o EAN. É uma requisição a mais por produto (como o Convertiez), então
`max_por_termo` limita o volume por rodada.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Iterator

import httpx

from .base import (Coletor, ItemColeta, ItemPerecivel, MercadoIndisponivel,
                   perecivel_se_fresco)
from .vtex import ean_valido

from analise import normalizar

log = logging.getLogger("coletor.gpa")

API = "https://api.vendas.gpa.digital/pa"
# "kg" vendido por peso = "kg" NÃO precedido de número ("Contra Filé Bovino Kg");
# "1,5kg"/"500g" é o peso da peça/bandeja (aí o preço da API é o da peça inteira)
_RE_POR_KG = re.compile(r"(?<![\d.,])\bkg\b", re.I)
IMG_BASE = "https://static-images.gpa.digital"  # thumbPath/productImages começam com /img/...
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class ColetorGPA(Coletor):
    plataforma = "gpa"

    def __init__(
        self, *, slug: str, nome: str, url_base: str, store_id: int = 461,
        cidade: str | None = None, delay: float = 0.6, timeout: float = 25.0,
        max_por_termo: int = 48, limite_por_rodada: int = 3000,
        contato: str = "contato@exemplo.com", **_ignorado,
    ) -> None:
        self.slug = slug
        self.nome = nome
        self.url_base = url_base.rstrip("/")
        self.store_id = int(store_id)
        self.cidade = cidade
        self.delay = max(delay, 0.0)
        self.timeout = timeout
        # cada produto custa 1 busca + 1 detalhe (EAN); limita o total por rodada.
        self.max_por_termo = min(max_por_termo, 48)
        self.limite_por_rodada = max(1, int(limite_por_rodada))
        self.contato = contato
        self._coletados = 0
        self._falhas_seguidas = 0
        self._max_falhas = 6  # disjuntor: aborta o mercado após N falhas seguidas
        self._ean_por_id: dict[str, str] = {}   # cache id do GPA -> EAN (links do banco)
        self._build_id: str | None = None       # buildId do Next.js do site (muda a cada deploy)
        self._ean_falhas = 0                    # detalhes seguidos sem EAN -> o site mudou
        self._max_ean_falhas = 25

    def carregar_cache_ean(self, conn, mercado_id: int) -> None:
        """id do produto -> EAN a partir dos links já gravados (url /produto/{id}/...):
        evita baixar a página de cada produto de novo todo dia."""
        for r in conn.execute("SELECT ean, url FROM links WHERE mercado_id = ?", (mercado_id,)):
            m = re.search(r"/produto/(\d+)", r["url"] or "")
            if m and r["ean"]:
                self._ean_por_id[m.group(1)] = r["ean"]
        log.info("[%s] cache de EAN: %d produtos já conhecidos.", self.slug, len(self._ean_por_id))

    def _headers(self) -> dict:
        return {"User-Agent": _UA, "Accept": "application/json",
                "Content-Type": "application/json", "Origin": self.url_base,
                "Referer": self.url_base + "/", "From": self.contato}

    def _req(self, client: httpx.Client, method: str, url: str, json=None,
             tentativas: int = 4) -> httpx.Response | None:
        """GET/POST com pacing, retry+backoff em 429/5xx e disjuntor (auto-cura;
        None se esgotar sem derrubar o mercado)."""
        espera = 2.0
        for i in range(tentativas):
            try:
                resp = client.request(method, url, json=json)
            except httpx.HTTPError as exc:
                if i == tentativas - 1:
                    log.warning("[%s] erro de rede: %s", self.slug, exc)
                    self._contar_falha()
                    return None
                time.sleep(espera); espera = min(espera * 2, 30); continue
            finally:
                time.sleep(self.delay)
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if i == tentativas - 1:
                    log.warning("[%s] HTTP %s após %d tentativas", self.slug, resp.status_code, tentativas)
                    self._contar_falha()
                    return resp
                ra = resp.headers.get("retry-after")
                pausa = float(ra) if (ra and ra.replace(".", "", 1).isdigit()) else espera
                time.sleep(min(pausa, 30)); espera = min(espera * 2, 30); continue
            self._falhas_seguidas = 0
            return resp
        return None

    def _contar_falha(self) -> None:
        self._falhas_seguidas += 1
        if self._falhas_seguidas >= self._max_falhas:
            raise MercadoIndisponivel(f"{self.slug}: {self._falhas_seguidas} falhas seguidas")

    def coletar(self, sementes: list[tuple[str, str]]) -> Iterator[ItemColeta]:
        vistos_ean: set[str] = set()
        vistos_id: set[str] = set()
        self._coletados = 0
        with httpx.Client(headers=self._headers(), timeout=self.timeout, follow_redirects=True) as client:
            for termo, categoria in sementes:
                if self._coletados >= self.limite_por_rodada:
                    log.info("[%s] limite da rodada (%d) atingido.", self.slug, self.limite_por_rodada)
                    return
                yield from self._buscar(client, termo, categoria, vistos_ean, vistos_id)

    def _buscar(self, client, termo, categoria, vistos_ean, vistos_id) -> Iterator[ItemColeta]:
        got = page = 0
        while got < self.max_por_termo:
            body = {"terms": termo, "page": page, "sortBy": "", "resultsPerPage": 24,
                    "allowRedirect": True, "storeId": self.store_id, "department": "ecom",
                    "customerPlus": True, "partner": "fallback"}
            r = self._req(client, "POST", f"{API}/search/search", json=body)
            if r is None or r.status_code != 200:
                return
            try:
                d = r.json()
            except ValueError:
                return
            prods = d.get("products") or []
            if not prods:
                return
            for p in prods:
                pid = str(p.get("id") or "")
                if not pid or pid in vistos_id:
                    continue
                vistos_id.add(pid)
                item = self._parse(client, p, categoria, vistos_ean)
                if item is not None:
                    got += 1
                    self._coletados += 1
                    yield item
                    if got >= self.max_por_termo or self._coletados >= self.limite_por_rodada:
                        return
            if page + 1 >= (d.get("totalPages") or 1):
                return
            page += 1

    def _parse(self, client, p, categoria, vistos_ean):
        pid = p.get("id")
        preco = p.get("price")
        if not pid or not preco or preco <= 0:
            return None
        if p.get("stock") is False:
            return None   # "Ops :( No momento não temos esse produto em estoque" no site
        nome = p.get("name")
        marca = p.get("brand")
        imgs = p.get("productImages") or []
        imagem = (IMG_BASE + imgs[0]) if (imgs and str(imgs[0]).startswith("/")) else (imgs[0] if imgs else None)
        url = p.get("urlDetails")
        por_peso = bool(p.get("variableWeight"))
        unidade = "kg" if por_peso else None

        ean = self._ean(client, pid)
        if ean and ean_valido(ean):
            if ean in vistos_ean:
                return None
            vistos_ean.add(ean)
            return ItemColeta(
                ean=ean, nome=nome, marca=marca, unidade=unidade, qtd_embalagem=None,
                imagem_url=imagem, preco=float(preco), categoria=categoria, url=url,
            )
        # Sem EAN universal: só entra como fresco se o nome for de fato fresco.
        # Item por peso: a API dá o preço da PEÇA/BANDEJA ("Contra Filé Swift 1,5kg" =
        # 104,85 = 69,90/kg). Com o peso no nome -> R$/kg; só "Kg" sem número -> já é
        # R$/kg; sem peso nenhum -> preço da peça, SEM "/kg" (não dá pra comparar por kg).
        preco = float(preco)
        if por_peso:
            medida = normalizar.extrair_medida(nome or "")
            if medida and medida.unidade == "kg" and medida.qtd_base > 0:
                preco = round(preco / medida.qtd_base, 2)
            elif not _RE_POR_KG.search(nome or ""):
                unidade = None
        return perecivel_se_fresco(
            cod_interno=str(pid), nome=nome, preco=preco, unidade=unidade,
            imagem_url=imagem, url=url,
        )

    def _ean(self, client, pid) -> str | None:
        pid = str(pid)
        if pid in self._ean_por_id:
            return self._ean_por_id[pid]
        ean = self._ean_do_site(client, pid)
        if ean:
            self._ean_falhas = 0
            self._ean_por_id[pid] = ean
            return ean
        # muitos detalhes seguidos sem EAN = o site mudou de novo: aborta logo (o robô sai
        # com erro e manda e-mail) em vez de ficar 1h30 batendo em 404 como em 06/11/2026
        self._ean_falhas += 1
        if self._ean_falhas >= self._max_ean_falhas:
            raise MercadoIndisponivel(f"{self.slug}: detalhe do produto sem EAN {self._ean_falhas}x seguidas")
        return None

    def _ean_do_site(self, client, pid: str) -> str | None:
        """EAN pelos dados da página do produto (Next.js). Sem buildId (ou se ele venceu),
        lê a página HTML, que já traz o EAN e o buildId novo."""
        if self._build_id:
            r = self._req(client, "GET", f"{self.url_base}/_next/data/{self._build_id}/produto/{pid}.json")
            if r is not None and r.status_code == 200:
                try:
                    prod = ((r.json().get("pageProps") or {}).get("product") or {})
                    return str(prod.get("ean") or "").strip() or None
                except ValueError:
                    pass
            self._build_id = None   # site publicou versão nova: pega o buildId de novo
        r = self._req(client, "GET", f"{self.url_base}/produto/{pid}")
        if r is None or r.status_code != 200:
            return None
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            return None
        try:
            nd = json.loads(m.group(1))
        except ValueError:
            return None
        self._build_id = nd.get("buildId")
        prod = ((nd.get("props") or {}).get("pageProps") or {}).get("product") or {}
        return str(prod.get("ean") or "").strip() or None

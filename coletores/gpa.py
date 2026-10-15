"""Coletor Pão de Açúcar (GPA) — API pública `api.vendas.gpa.digital/pa` (sem auth).

Descoberto por engenharia reversa do front (Next.js): a busca é
    POST /pa/search/search  {terms, page, resultsPerPage, storeId, department:'ecom',
                             customerPlus, partner:'fallback', allowRedirect}
que devolve produtos com PREÇO (mas sem EAN). O EAN vem do detalhe:
    GET  /pa/v4/products/ecom/{id}?storeId={store}   -> content.ean

Como a busca é por TERMO (não há varredura por categoria acessível), este coletor usa
o modo termos-semente do projeto (coletar(sementes)): busca cada termo e, pra cada
produto, consulta o EAN. É uma requisição a mais por produto (como o Convertiez), então
`max_por_termo` limita o volume por rodada.
"""
from __future__ import annotations

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
        r = self._req(client, "GET", f"{API}/v4/products/ecom/{pid}?storeId={self.store_id}")
        if r is not None and r.status_code == 200:
            try:
                return str((r.json().get("content") or {}).get("ean") or "").strip()
            except ValueError:
                return None
        return None

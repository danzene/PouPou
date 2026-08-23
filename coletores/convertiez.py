"""Coletor Convertiez (plataforma do Super Pague Menos e outros supermercados).

A Convertiez não expõe o EAN na listagem de busca — só na PÁGINA de cada produto
(`itemprop="gtin13"`). Então este é um coletor de FALLBACK HTML, mais pesado:
  1) GET /busca/?q={termo}      -> extrai as URLs de produto (…/p) da listagem
  2) GET /{slug}/p (por produto) -> extrai gtin13 (EAN), nome, preço, imagem, marca

É mais lento (1 busca + N páginas por termo) e mais frágil que uma API JSON, então
limitamos os produtos por termo (`max_por_busca`) e mantemos o pacing.
"""
from __future__ import annotations

import html as _html
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

import httpx

from .base import Coletor, ItemColeta, ItemPerecivel, robots_permite
from .vtex import ean_valido

log = logging.getLogger("coletor.convertiez")


class BloqueioDetectado(Exception):
    """O site derrubou várias requisições seguidas (bloqueio/anti-bot). Sobe pra ABORTAR
    a rodada na hora, em vez de martelar um site bloqueado (foi o que causou o IP block)."""

RAIZ = Path(__file__).resolve().parent.parent
_RE_LOC = re.compile(r"<loc>([^<]+)</loc>", re.I)
# 'kg' vendido por peso = 'kg' NÃO precedido de número (senão é '1kg' = pacote fixo)
_RE_POR_KG = re.compile(r"(?<![\d.,])\bkg\b")
# gramatura fixa de pacote: '200g', '1,02 kg', '500 g', '1l', '355ml'…
_RE_PACOTE = re.compile(r"\d[\d.,]*\s*(kg|g|ml|l)\b")
# peso de referência do item por peso (data attr: weight = '1.80000000')
_RE_WEIGHT = re.compile(r"weight'?\s*[:=]\s*'([\d.]+)'")
# selo "SÓ NO SITE" (exclusivo online) = imagem selo_exclusividade
_RE_SO_SITE = re.compile(r"selo_exclusividade", re.I)
# % de desconto, só quando o bloco `discount` está VISÍVEL (não display:none)
_RE_DESCONTO = re.compile(r'class="discount"(?![^>]*display:none).*?<b>\s*(\d+)\s*%', re.S | re.I)
# preço "de" (antes do desconto) no bloco unit-price
_RE_UNIT_PRICE = re.compile(r'class="unit-price"[^>]*>\s*<span>\s*R\$\s*([\d.,]+)', re.I)


def _extrair_promo(html: str) -> tuple[bool, float | None, int | None]:
    """(so_no_site, preco_de_CRU, desconto_pct) a partir dos selos/preço da página.
    O preco_de vem no valor total da página; quem chama divide pelo peso se for item
    por peso (pra ficar por-kg como o preço) e só usa se for > preço atual."""
    so = bool(_RE_SO_SITE.search(html))
    md = _RE_DESCONTO.search(html)
    pct = int(md.group(1)) if md and int(md.group(1)) > 0 else None
    de = None
    if pct:
        mu = _RE_UNIT_PRICE.search(html)
        if mu:
            try:
                de = float(mu.group(1).replace(".", "").replace(",", "."))
            except ValueError:
                de = None
    return so, de, pct

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 MercadoBaratoBot/0.1")

_RE_PROD_HREF = re.compile(r'href="(/[a-z0-9][a-z0-9\-/]*?/p)"', re.I)
_RE_EAN = re.compile(r'itemprop="gtin1?3?"\s+content="(\d{8,14})"', re.I)
_RE_NOME = re.compile(r'property="og:title"\s+content="([^"]+)"', re.I)
_RE_PRECO = re.compile(r'itemprop="price"[^>]*content="([\d.]+)"', re.I)
_RE_IMG = re.compile(r'property="og:image"\s+content="([^"]+)"', re.I)
_RE_MARCA = re.compile(r'itemprop="brand"[^>]*content="([^"]+)"', re.I)


class ColetorConvertiez(Coletor):
    plataforma = "convertiez"

    def __init__(
        self, *, slug: str, nome: str, url_base: str, cidade: str | None = None,
        delay: float = 1.2, timeout: float = 25.0, max_por_termo: int = 50,
        max_por_busca: int = 12, limite_por_rodada: int = 3000,
        contato: str = "contato@exemplo.com", **_ignorado,
    ) -> None:
        self.slug = slug
        self.nome = nome
        self.url_base = url_base.rstrip("/")
        self.cidade = cidade
        self.delay = max(delay, 0.0)
        self.timeout = timeout
        self.max_por_termo = max_por_termo
        self.max_por_busca = max_por_busca
        self.limite_por_rodada = max(1, limite_por_rodada)
        self.contato = contato
        # disjuntor anti-bloqueio: N falhas de REDE seguidas -> aborta a rodada
        self._falhas_seguidas = 0
        self._max_falhas_seguidas = 6

    def _headers(self) -> dict:
        return {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml,*/*",
                "Accept-Language": "pt-BR,pt;q=0.9", "From": self.contato}

    # ------------------------------------------------------------------ #
    #  Catálogo COMPLETO via sitemap (preferido). Como cada página é uma  #
    #  requisição (o EAN só está na página), varremos em FATIAS rotativas #
    #  por rodada — o catálogo inteiro é coberto ao longo de poucos dias  #
    #  e segue sendo re-atualizado, sem estourar o job diário.            #
    # ------------------------------------------------------------------ #
    @property
    def _arquivo_offset(self) -> Path:
        return RAIZ / "dados" / f"convertiez_offset_{self.slug}.json"

    def _ler_offset(self) -> int:
        try:
            return int(json.loads(self._arquivo_offset.read_text(encoding="utf-8"))["offset"])
        except (OSError, ValueError, KeyError):
            return 0

    def _gravar_offset(self, offset: int, total: int) -> None:
        try:
            self._arquivo_offset.parent.mkdir(parents=True, exist_ok=True)
            self._arquivo_offset.write_text(
                json.dumps({"offset": offset, "total": total}), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("[%s] não consegui gravar offset: %s", self.slug, exc)

    def _urls_sitemap(self, client: httpx.Client) -> list[str]:
        """Índice de sitemaps -> sub-sitemaps de produtos -> URLs de produto (…/p)."""
        indice = self._get(client, f"{self.url_base}/s/{self.slug}/sitemap.xml")
        if indice is None or indice.status_code != 200:
            return []
        sub = [u for u in _RE_LOC.findall(indice.text) if "sitemap-products" in u]
        urls: list[str] = []
        for u in sub:
            resp = self._get(client, u)
            if resp is None or resp.status_code != 200:
                continue
            urls.extend(x for x in _RE_LOC.findall(resp.text) if x.endswith("/p"))
        urls = sorted(set(urls))  # ordem estável entre rodadas (o offset faz sentido)
        return urls

    def coletar_catalogo(self) -> Iterator[ItemColeta]:
        headers = self._headers()
        with httpx.Client(headers=headers, timeout=self.timeout, follow_redirects=True) as client:
            urls = self._urls_sitemap(client)
            if not urls:
                log.warning("[%s] sitemap vazio/indisponível — caindo p/ termos-semente.", self.slug)
                return
            total = len(urls)
            if not robots_permite(self.url_base, "/produto/p", headers["User-Agent"], headers):
                log.warning("[%s] robots.txt bloqueia páginas de produto — pulando.", self.slug)
                return
            if total <= self.limite_por_rodada:
                # catálogo inteiro cabe numa rodada — varre tudo e zera o offset
                # (evita lacuna permanente se o total encolher e o offset ficar preso)
                offset, fatia, proximo = 0, urls, 0
            else:
                offset = self._ler_offset() % total
                fatia = urls[offset : offset + self.limite_por_rodada]
                if len(fatia) < self.limite_por_rodada:
                    fatia += urls[: self.limite_por_rodada - len(fatia)]  # dá a volta
                proximo = (offset + self.limite_por_rodada) % total
            log.info("[%s] sitemap: %d produtos | rodada varre %d a partir do offset %d",
                     self.slug, total, len(fatia), offset)
            for url in fatia:
                href = url[len(self.url_base):] if url.startswith(self.url_base) else None
                if not href:
                    continue
                item = self._produto(client, href, None)  # categoria herdada por EAN de outros mercados
                if item is not None:
                    yield item
            self._gravar_offset(proximo, total)

    def coletar(self, sementes: list[tuple[str, str]]) -> Iterator[ItemColeta]:
        headers = self._headers()
        if not robots_permite(self.url_base, "/busca/", headers["User-Agent"], headers):
            log.warning("[%s] robots.txt não permite /busca/ — pulando.", self.slug)
            return
        with httpx.Client(headers=headers, timeout=self.timeout, follow_redirects=True) as client:
            for termo, categoria in sementes:
                yield from self._buscar_termo(client, termo, categoria)

    def _get(self, client: httpx.Client, url: str, tentativas: int = 4) -> httpx.Response | None:
        """GET com DISJUNTOR: conta falhas de rede seguidas e ABORTA a rodada
        (BloqueioDetectado) quando o site está derrubando tudo — assim a coleta nunca
        fica martelando um site bloqueado (foi isso que causou o bloqueio do IP)."""
        resp = self._get_raw(client, url, tentativas)
        if resp is None:
            self._falhas_seguidas += 1
            if self._falhas_seguidas >= self._max_falhas_seguidas:
                raise BloqueioDetectado(
                    f"{self._falhas_seguidas} falhas de rede seguidas no {self.slug} — "
                    "site bloqueando; abortando a rodada pra não piorar.")
        else:
            self._falhas_seguidas = 0
        return resp

    def _get_raw(self, client: httpx.Client, url: str, tentativas: int = 4) -> httpx.Response | None:
        """GET com pacing (com jitter) e retry+backoff em 429/5xx (auto-cura rate-limit)."""
        espera = 2.0
        for i in range(tentativas):
            try:
                resp = client.get(url)
            except httpx.HTTPError as exc:
                if i == tentativas - 1:
                    log.warning("[%s] erro de rede %s: %s", self.slug, url[-40:], exc)
                    return None
                time.sleep(espera); espera = min(espera * 2, 30); continue
            finally:
                time.sleep(self.delay + random.uniform(0, self.delay * 0.7))  # jitter: menos "robótico"
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if i == tentativas - 1:
                    return resp
                ra = resp.headers.get("retry-after")
                pausa = float(ra) if (ra and ra.replace(".", "", 1).isdigit()) else espera
                time.sleep(min(pausa, 30)); espera = min(espera * 2, 30); continue
            return resp
        return resp

    def _buscar_termo(self, client: httpx.Client, termo: str, categoria: str | None) -> Iterator[ItemColeta]:
        resp = self._get(client, f"{self.url_base}/busca/?q={quote(termo, safe='')}")
        if resp is None or resp.status_code != 200:
            return
        # URLs de produto (…/p), únicas, na ordem, limitadas
        hrefs: list[str] = []
        vistos = set()
        for h in _RE_PROD_HREF.findall(resp.text):
            if h not in vistos:
                vistos.add(h)
                hrefs.append(h)
            if len(hrefs) >= min(self.max_por_busca, self.max_por_termo):
                break

        for href in hrefs:
            item = self._produto(client, href, categoria)
            if item is not None:
                yield item

    def _produto(
        self, client: httpx.Client, href: str, categoria: str | None
    ) -> ItemColeta | ItemPerecivel | None:
        url = self.url_base + href
        resp = self._get(client, url)
        if resp is None or resp.status_code != 200:
            return None
        html = resp.text
        m_preco = _RE_PRECO.search(html)
        if not m_preco:
            return None
        preco = float(m_preco.group(1))
        if preco <= 0:
            return None
        m_nome = _RE_NOME.search(html)
        nome = _html.unescape(m_nome.group(1)) if m_nome else None  # 'Cenoura&amp;Br' -> 'Cenoura&Br'
        img = _RE_IMG.search(html)
        imagem_url = img.group(1).replace("http://", "https://") if img else None

        so_site, de_raw, desc_pct = _extrair_promo(html)

        m_ean = _RE_EAN.search(html)
        if m_ean and ean_valido(m_ean.group(1)):
            marca = _RE_MARCA.search(html)
            preco_de = de_raw if (de_raw and de_raw > preco) else None
            return ItemColeta(
                ean=m_ean.group(1),
                nome=nome,
                marca=marca.group(1) if marca else None,
                unidade=None,
                qtd_embalagem=None,
                imagem_url=imagem_url,
                preco=preco,
                categoria=categoria,
                url=url,
                so_no_site=so_site,
                preco_de=round(preco_de, 2) if preco_de else None,
                desconto_pct=desc_pct,
            )
        # Sem EAN universal: item FRESCO/por peso (açougue, hortifrúti, padaria).
        # Vai pra tabela `pereciveis` (cruzado por nome, aproximado), não pro fluxo por EAN.
        if not nome:
            return None
        cod = href.strip("/")
        if cod.endswith("/p"):
            cod = cod[:-2].rstrip("/")
        # Item VENDIDO POR PESO (açougue/hortifrúti a granel): a página tem o bloco
        # "Preço por KG" (class price-weight) e o `itemprop=price` é o TOTAL da peça de
        # referência (peso `weight` kg). O preço/kg REAL = total ÷ peso — é o que o site
        # calcula no JS e o que compara entre mercados. Ex.: picanha 233,98 / 1,8 = 129,99/kg.
        m_peso = _RE_WEIGHT.search(html)
        peso = None
        if m_peso:
            try:
                peso = float(m_peso.group(1))
            except ValueError:
                peso = None
        por_peso = "price-weight" in html and peso and peso > 0
        if por_peso:
            preco = round(preco / peso, 2)
            unid = "kg"
        else:
            unid = self._unidade_heuristica(nome, cod)
        # Descarta lixo de configuração da fonte (item por peso com preço/kg irreal,
        # ex.: bacon com weight=1 e meta=0,04 → o próprio site mostra R$0,04).
        if unid == "kg" and preco < 1.0:
            return None
        # preço "de" na mesma escala do preço atual (por-kg se for item por peso)
        preco_de = None
        if de_raw:
            de_val = round(de_raw / peso, 2) if por_peso else de_raw
            if de_val > preco:
                preco_de = round(de_val, 2)
        return ItemPerecivel(
            cod_interno=cod or href,
            nome=nome,
            preco=preco,
            unidade=unid,
            imagem_url=imagem_url,
            url=url,
            so_no_site=so_site,
            preco_de=preco_de,
            desconto_pct=desc_pct,
        )

    @staticmethod
    def _unidade_heuristica(nome: str, cod: str) -> str | None:
        """Unidade de VENDA pelo nome/slug. 'kg' só quando vendido por peso — um 'kg'/'g'
        precedido de número (ex.: '200g', '1,02kg') é tamanho de PACOTE, não venda a granel
        (nesse caso o preço é do pacote e não leva sufixo)."""
        alvo = f"{nome} {cod}".lower()
        if _RE_PACOTE.search(alvo):     # gramatura fixa ('200g','1 kg') vence: preço é do pacote
            return None
        if "granel" in alvo or _RE_POR_KG.search(alvo):
            return "kg"
        if any(t in alvo for t in ("unidade", "duzia", "dúzia", "bandeja", " un ", "-un")):
            return "un"
        return None

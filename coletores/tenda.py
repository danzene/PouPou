"""Coletor do Tenda Atacado — plataforma própria (Next.js + API custom).

A API fica em api.tendaatacado.com.br/api e exige um bearer anônimo no header
`X-Authorization`. O token é gerado dinamicamente em 2 passos (fluxo do próprio
front, com credenciais públicas embutidas):
  1) POST /public/anonymous-client            -> {user, password} (credenciais anônimas)
  2) POST /public/oauth/access-token          -> {access_token} (grant_type=password)
Depois:
  GET  /public/store/search?query={termo}&page=N&perPage=  (com X-Authorization)

Campos: barcode -> ean | name -> nome | price -> preco | url -> link | thumbnail | brand
Token expira em ~3h; geramos um por coleta.
"""
from __future__ import annotations

import logging
import time
from typing import Iterator
from urllib.parse import quote

import httpx

from .base import Coletor, ItemColeta, chave_dedup, perecivel_se_fresco
from .vtex import ean_valido

log = logging.getLogger("coletor.tenda")

API = "https://api.tendaatacado.com.br/api"
# Credenciais públicas do client OAuth (embutidas no JS do front — password grant).
CLIENT_ID = "kvlp9ojse3yvepnu3jcs97gws4esu1g9"
CLIENT_SECRET = "qxlh237yndwy4ejy26ko8liioh53ign2nndvsix7i2qi58y413ef8wepxpz4zzs7"
MAX_PAGINAS = 5
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 MercadoBaratoBot/0.1")


def mintar_token(timeout: float = 25.0) -> str | None:
    """Gera um bearer anônimo: anonymous-client -> oauth. Devolve access_token ou None."""
    h = {"User-Agent": _UA, "Accept": "application/json",
         "Origin": "https://www.tendaatacado.com.br", "Referer": "https://www.tendaatacado.com.br/"}
    try:
        r = httpx.post(f"{API}/public/anonymous-client", headers=h, timeout=timeout)
        if r.status_code != 201:
            log.warning("Tenda anonymous-client -> %s", r.status_code)
            return None
        cred = r.json()
        body = {"username": cred["user"], "password": cred["password"],
                "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "grant_type": "password"}
        r2 = httpx.post(f"{API}/public/oauth/access-token?g-recaptcha-response=",
                        headers={**h, "Content-Type": "application/x-www-form-urlencoded"},
                        data=body, timeout=timeout)
        if r2.status_code != 200:
            log.warning("Tenda oauth -> %s: %s", r2.status_code, r2.text[:100])
            return None
        return r2.json().get("access_token")
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.warning("Tenda: falha ao gerar token: %s", exc)
        return None


class ColetorTenda(Coletor):
    plataforma = "tenda"

    def __init__(
        self, *, slug: str, nome: str, url_base: str, cidade: str | None = None,
        delay: float = 1.2, timeout: float = 25.0, max_por_termo: int = 50,
        contato: str = "contato@exemplo.com", **_ignorado,
    ) -> None:
        self.slug = slug
        self.nome = nome
        self.url_base = url_base.rstrip("/")
        self.cidade = cidade
        self.delay = max(delay, 0.0)
        self.timeout = timeout
        self.max_por_termo = max_por_termo
        self.contato = contato

    def _headers(self, token: str) -> dict:
        return {"User-Agent": _UA, "Accept": "application/json", "Accept-Language": "pt-BR,pt;q=0.9",
                "Origin": self.url_base, "Referer": self.url_base + "/",
                "X-Authorization": f"Bearer {token}", "From": self.contato}

    def coletar(self, sementes: list[tuple[str, str]]) -> Iterator[ItemColeta]:
        token = mintar_token(self.timeout)
        if not token:
            log.warning("[%s] não obtive token — pulando mercado.", self.slug)
            return
        with httpx.Client(headers=self._headers(token), timeout=self.timeout, follow_redirects=True) as client:
            for termo, categoria in sementes:
                yield from self._buscar_termo(client, termo, categoria)

    # -- varredura de CATÁLOGO COMPLETO (por árvore de categorias) ---------
    def coletar_catalogo(self) -> Iterator[ItemColeta]:
        """Varre TODO o catálogo pela árvore de categorias (não por termos-semente).

        Pagina cada nó VITRINE (o pai NÃO é superset dos filhos — produtos podem estar
        fixados só no pai). Otimização: pula um pai VITRINE quando ele não tem produtos
        exclusivos (countProducts <= soma dos filhos). Dedup por EAN.
        """
        from .mapa_categorias import mapear_categoria

        token = mintar_token(self.timeout)
        if not token:
            log.warning("[%s] não obtive token — pulando mercado.", self.slug)
            return
        with httpx.Client(headers=self._headers(token), timeout=self.timeout, follow_redirects=True) as client:
            resp = self._get(client, f"{API}/public/store/all-categories")
            try:
                arvore = resp.json() if resp is not None else None
            except ValueError:
                arvore = None
            if not arvore:
                log.warning("[%s] não obtive a árvore de categorias (mercado defasado — "
                            "preço antigo mantido e marcado como 'há X dias').", self.slug)
                return
            vistos: set = set()
            for dep in (arvore.get("children") or []):
                categoria = mapear_categoria(dep.get("name"))
                for vid in self._vitrines_para_varrer(dep):
                    yield from self._varrer_vitrine(client, vid, categoria, vistos)

    def _get(self, client: httpx.Client, url: str, tentativas: int = 4) -> httpx.Response | None:
        """GET com pacing e retry+backoff em 429/5xx (auto-cura rate-limit).
        None se esgotar — o mercado não é derrubado, o preço antigo fica."""
        espera = 2.0
        for i in range(tentativas):
            try:
                resp = client.get(url)
            except httpx.HTTPError as exc:
                if i == tentativas - 1:
                    log.warning("[%s] erro de rede: %s", self.slug, exc)
                    return None
                time.sleep(espera); espera = min(espera * 2, 30); continue
            finally:
                time.sleep(self.delay)
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if i == tentativas - 1:
                    return resp
                ra = resp.headers.get("retry-after")
                pausa = float(ra) if (ra and ra.replace(".", "", 1).isdigit()) else espera
                time.sleep(min(pausa, 30)); espera = min(espera * 2, 30); continue
            return resp
        return resp

    @staticmethod
    def _vitrines_para_varrer(node) -> Iterator[int]:
        """IDs dos nós VITRINE que valem paginar: folhas + pais com produtos exclusivos."""
        filhos = node.get("children") or []
        filhos_vitrine = [c for c in filhos if c.get("type") == "VITRINE"]
        if node.get("type") == "VITRINE" and node.get("id") != 1:
            soma = sum(c.get("countProducts") or 0 for c in filhos_vitrine)
            if not filhos_vitrine or (node.get("countProducts") or 0) > soma:
                yield node["id"]
        for c in filhos:
            yield from ColetorTenda._vitrines_para_varrer(c)

    def _varrer_vitrine(self, client, cid, categoria, vistos) -> Iterator[ItemColeta]:
        page = 1
        while True:
            url = f"{API}/public/store/category/{cid}/products?page={page}&perPage=50"
            resp = self._get(client, url)
            if resp is None or resp.status_code != 200 or "json" not in (resp.headers.get("content-type") or ""):
                return
            data = resp.json()
            prods = data.get("products") or []
            if not prods:
                return
            for prod in prods:
                item = self._parse(prod, categoria)
                if item is None:
                    continue
                k = chave_dedup(item)
                if k in vistos:
                    continue
                vistos.add(k)
                yield item
            if page >= (data.get("total_pages") or page):
                return
            page += 1

    def _buscar_termo(self, client: httpx.Client, termo: str, categoria: str | None) -> Iterator[ItemColeta]:
        vistos = 0
        for page in range(1, MAX_PAGINAS + 1):
            if vistos >= self.max_por_termo:
                return
            url = f"{API}/public/store/search?query={quote(termo, safe='')}&page={page}&perPage=50"
            resp = self._get(client, url)
            if resp is None:
                return
            if resp.status_code != 200 or "json" not in (resp.headers.get("content-type") or ""):
                log.warning("[%s] '%s' p%d -> %s; parando termo.", self.slug, termo, page, resp.status_code)
                return
            data = resp.json()
            produtos = data.get("products") or []
            if not produtos:
                return
            for prod in produtos:
                item = self._parse(prod, categoria)
                if item is not None:
                    vistos += 1
                    yield item
            if page >= (data.get("total_pages") or page):
                return

    def _parse(self, prod: dict, categoria: str | None):
        preco = prod.get("price")
        if not preco or preco <= 0:
            return None
        if prod.get("availability") not in (None, "in_stock"):
            return None
        preco = float(preco)
        # preço "de" (promoção): a Tenda pode expor em campos variados — tenta os comuns
        de = (prod.get("oldPrice") or prod.get("priceFrom") or prod.get("originalPrice")
              or prod.get("listPrice") or prod.get("price_from"))
        preco_de = desconto_pct = None
        try:
            de = float(de) if de else None
        except (TypeError, ValueError):
            de = None
        if de and de > preco:
            preco_de = round(de, 2)
            desconto_pct = round((preco_de - preco) / preco_de * 100)
            if desconto_pct <= 0:
                preco_de = desconto_pct = None
        nome = prod.get("name")
        imagem = prod.get("thumbnail")
        url = prod.get("url")

        ean = str(prod.get("barcode") or "").strip()
        if not ean_valido(ean):
            # Sem código de barras: se for fresco (açougue/hortifrúti/...), vira perecível.
            cod = str(prod.get("id") or prod.get("sku") or prod.get("slug") or url or "").strip()
            return perecivel_se_fresco(
                cod_interno=cod, nome=nome, preco=preco, unidade=None,
                imagem_url=imagem, url=url, preco_de=preco_de, desconto_pct=desconto_pct,
            )
        return ItemColeta(
            ean=ean,
            nome=nome,
            marca=prod.get("brand"),
            unidade=None,           # normalização extrai do nome
            qtd_embalagem=None,
            imagem_url=imagem,
            preco=preco,
            categoria=categoria,
            url=url,
            preco_de=preco_de,
            desconto_pct=desconto_pct,
        )

"""Coletor genérico VTEX — reaproveitável entre todos os mercados VTEX.

Consome a API pública de catálogo (JSON estruturado), nunca HTML:
    GET {url_base}/api/catalog_system/pub/products/search/?ft={termo}&_from=&_to=

Só mantém itens COM EAN (industrializados com código de barras); isso já
exclui granel/hortifrúti naturalmente. Preço = primeiro seller disponível.
"""
from __future__ import annotations

import logging
import time
from typing import Iterator
from urllib.parse import quote

import httpx

from .base import (Coletor, ItemColeta, MercadoIndisponivel, chave_dedup,
                   perecivel_se_fresco, robots_permite)

log = logging.getLogger("coletor.vtex")

CAMINHO_BUSCA = "/api/catalog_system/pub/products/search/"
CAMINHO_BUSCA_FQ = "/api/catalog_system/pub/products/search"  # varredura por categoria (fq=C)
CAMINHO_ARVORE = "/api/catalog_system/pub/category/tree/50"
JANELA = 50        # VTEX limita a janela _from/_to a 50 itens por página
OFFSET_MAX = 2500  # a VTEX recusa _from > 2500 (HTTP 400) — por isso a varredura desce por categoria

# GTIN válido: só dígitos, comprimento 8/12/13/14, e não todo-zero.
# A VTEX devolve ean="0" (ou lixo) para itens sem código de barras real;
# deixá-los entrar faria produtos diferentes colidirem na mesma chave.
_GTIN_LENS = {8, 12, 13, 14}


def ean_valido(ean: str) -> bool:
    return ean.isdigit() and len(ean) in _GTIN_LENS and int(ean) != 0

# Headers de NAVEGADOR (a API pública de catálogo é a mesma que o site serve ao
# navegador). Os WAFs da VTEX (Savegnago/Covabra) devolvem 429 quando o User-Agent
# tem marcador de bot ("...Bot/0.1") ou headers From/X-Bot-Contato — então usamos um
# UA de Chrome limpo. A coleta segue responsável: robots.txt é respeitado, há pacing
# entre requisições (self.delay) e retry com backoff (não martela em cima de 429).
def _headers(contato: str) -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.google.com/",
    }


class ColetorVTEX(Coletor):
    plataforma = "vtex"

    def __init__(
        self,
        *,
        slug: str,
        nome: str,
        url_base: str,
        cidade: str | None = None,
        sales_channel: int | None = None,
        delay: float = 1.2,
        timeout: float = 20.0,
        max_por_termo: int = 50,
        contato: str = "contato@exemplo.com",
    ) -> None:
        self.slug = slug
        self.nome = nome
        self.url_base = url_base.rstrip("/")
        self.cidade = cidade
        self.sales_channel = sales_channel
        self.delay = max(delay, 0.0)
        self.timeout = timeout
        self.max_por_termo = max_por_termo
        self.contato = contato
        self._falhas_seguidas = 0   # disjuntor: falhas HTTP consecutivas
        self._max_falhas = 12       # aborta o mercado só após MUITAS falhas seguidas
                                    # (WAFs bursty devolvem rachos de 429 antes de voltar;
                                    #  6 era baixo demais e derrubava o mercado no meio)

    # -- requisição resiliente -------------------------------------------
    def _get(self, client: httpx.Client, url: str, tentativas: int = 5) -> httpx.Response | None:
        """GET com retry+backoff curto em 429/5xx (auto-cura rate-limit transitório).
        DISJUNTOR: se o mercado falha muitas vezes SEGUIDAS (rate-limit persistente),
        levanta MercadoIndisponivel pra ABORTAR o mercado rápido em vez de pendurar
        horas — o preço antigo fica e o app o marca como 'há X dias'. Sucesso zera o
        contador; num uso normal (coleta 1x/dia) o disjuntor nunca dispara."""
        espera = 2.0
        for i in range(tentativas):
            try:
                r = client.get(url)
            except httpx.HTTPError as exc:
                if i == tentativas - 1:
                    log.warning("[%s] rede falhou (%s): %s", self.slug, url[-60:], exc)
                    break
                time.sleep(espera); espera = min(espera * 2, 20); continue
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if i == tentativas - 1:
                    log.warning("[%s] HTTP %s após %d tentativas", self.slug, r.status_code, tentativas)
                    break
                ra = r.headers.get("retry-after")
                pausa = float(ra) if (ra and ra.replace(".", "", 1).isdigit()) else espera
                time.sleep(min(pausa, 20)); espera = min(espera * 2, 20); continue
            self._falhas_seguidas = 0
            return r
        # esgotou as tentativas: conta a falha e decide se abre o disjuntor
        self._falhas_seguidas += 1
        if self._falhas_seguidas >= self._max_falhas:
            raise MercadoIndisponivel(
                f"{self.slug}: {self._falhas_seguidas} falhas seguidas (rate-limit) — abortando "
                f"o mercado pra não travar; preço antigo é mantido e marcado como defasado.")
        return None

    # -- interface --------------------------------------------------------
    def coletar(self, sementes: list[tuple[str, str]]) -> Iterator[ItemColeta]:
        headers = _headers(self.contato)
        if not robots_permite(self.url_base, CAMINHO_BUSCA, headers["User-Agent"], headers):
            log.warning("[%s] robots.txt não permite — pulando mercado.", self.slug)
            return

        with httpx.Client(headers=headers, timeout=self.timeout, follow_redirects=True) as client:
            for termo, categoria in sementes:
                yield from self._buscar_termo(client, termo, categoria)

    # -- varredura de CATÁLOGO COMPLETO (por árvore de categorias) ---------
    def coletar_catalogo(self) -> Iterator[ItemColeta]:
        """Varre TODO o catálogo pela árvore de categorias (não por termos-semente).

        Para cada categoria pagina com fq=C:{caminho completo de ids}; se a categoria
        estoura o teto de offset (2500), desce recursivamente nos filhos. Dedup por EAN.
        """
        from .mapa_categorias import mapear_categoria

        headers = _headers(self.contato)
        if not robots_permite(self.url_base, CAMINHO_BUSCA_FQ, headers["User-Agent"], headers):
            log.warning("[%s] robots.txt não permite — pulando mercado.", self.slug)
            return
        with httpx.Client(headers=headers, timeout=self.timeout, follow_redirects=True) as client:
            resp = self._get(client, self.url_base + CAMINHO_ARVORE)
            time.sleep(self.delay)
            try:
                arvore = resp.json() if resp is not None else None
            except ValueError as exc:
                log.warning("[%s] árvore de categorias não veio em JSON: %s", self.slug, exc)
                arvore = None
            if not arvore:
                log.warning("[%s] não obtive a árvore de categorias (mercado ficou defasado — "
                            "preço antigo é mantido e marcado como 'há X dias' no app).", self.slug)
                return
            vistos: set[str] = set()
            for dep in (arvore or []):
                categoria = mapear_categoria(dep.get("name"))
                yield from self._varrer_categoria(client, dep, "", categoria, vistos)

    def _varrer_categoria(self, client, node, prefixo, categoria, vistos) -> Iterator[ItemColeta]:
        fqpath = f"{prefixo}{node['id']}"
        total = self._total_categoria(client, fqpath)
        filhos = node.get("children") or []
        # Desce nos filhos quando (a) a categoria estoura o teto de offset (2500) e tem
        # filhos, OU (b) o total é DESCONHECIDO (a consulta falhou) e há filhos — assim
        # um único 429 numa categoria-pai não poda o galho inteiro (era a causa de só
        # ~7% do catálogo entrar). Categorias-pai com total conhecido <= 2550 são
        # paginadas direto (o fq=C:pai já traz a subárvore inteira).
        if filhos and (total < 0 or total > (OFFSET_MAX + JANELA)):
            for filho in filhos:
                yield from self._varrer_categoria(client, filho, fqpath + "/", categoria, vistos)
        else:
            # folha com total desconhecido: pagina até a página vazia/curta (o teto de
            # offset e a detecção de página curta param no fim real, então inflar é seguro).
            alvo = total if total >= 0 else (OFFSET_MAX + JANELA)
            yield from self._paginar_categoria(client, fqpath, alvo, categoria, vistos)

    def _total_categoria(self, client, fqpath: str) -> int:
        """Total de itens da categoria (header 'resources': '0-49/1234' -> 1234).
        Devolve -1 quando o total é DESCONHECIDO (requisição falhou por rate-limit,
        ou o header não veio) — distinto de 0 (categoria realmente vazia). Quem chama
        NUNCA deve podar um galho inteiro por causa de uma consulta que só falhou."""
        url = f"{self.url_base}{CAMINHO_BUSCA_FQ}?fq=C:{fqpath}&_from=0&_to=0"
        r = self._get(client, url)
        time.sleep(self.delay)
        if r is None:
            return -1
        res = r.headers.get("resources") or ""
        if "/" not in res:
            return -1  # header ausente/inesperado -> desconhecido (não poda o galho)
        try:
            return int(res.split("/")[-1])
        except ValueError:
            return -1

    def _paginar_categoria(self, client, fqpath, total, categoria, vistos) -> Iterator[ItemColeta]:
        frm = 0
        while frm < total and frm <= OFFSET_MAX:
            to = frm + JANELA - 1
            url = f"{self.url_base}{CAMINHO_BUSCA_FQ}?fq=C:{fqpath}&_from={frm}&_to={to}"
            resp = self._get(client, url)
            time.sleep(self.delay)
            if resp is None or resp.status_code not in (200, 206):
                return
            try:
                lote = resp.json()
            except ValueError:
                return
            if not lote:
                return
            for prod in lote:
                item = self._parse_produto(prod)
                if item is None:
                    continue
                k = chave_dedup(item)
                if k in vistos:
                    continue
                vistos.add(k)
                if isinstance(item, ItemColeta):
                    item.categoria = categoria
                yield item
            if len(lote) < JANELA:
                return
            frm += JANELA

    # -- interno ----------------------------------------------------------
    def _buscar_termo(self, client: httpx.Client, termo: str, categoria: str | None) -> Iterator[ItemColeta]:
        _from = 0
        while _from < self.max_por_termo:
            _to = min(_from + JANELA - 1, self.max_por_termo - 1)
            # Monta a query à mão: o `ft` PRECISA usar %20 (o `+` do quote_plus/httpx
            # dispara 400 no WAF do Savegnago). Espaço -> %20 via quote(safe='').
            query = f"ft={quote(termo, safe='')}&_from={_from}&_to={_to}"
            if self.sales_channel is not None:
                query += f"&sc={self.sales_channel}"
            url = f"{self.url_base}{CAMINHO_BUSCA}?{query}"
            resp = self._get(client, url)
            time.sleep(self.delay)  # pacing SEMPRE
            if resp is None:
                return
            if resp.status_code not in (200, 206):
                log.warning("[%s] '%s' devolveu %s — parando esse termo.", self.slug, termo, resp.status_code)
                return

            try:
                produtos = resp.json()
            except ValueError:
                log.warning("[%s] '%s' não devolveu JSON — provável soft-404/não-VTEX.", self.slug, termo)
                return
            if not produtos:
                return

            for prod in produtos:
                item = self._parse_produto(prod)
                if item is None:
                    continue
                if isinstance(item, ItemColeta):
                    item.categoria = categoria
                yield item

            if len(produtos) < (_to - _from + 1):
                return  # última página
            _from += JANELA

    def _parse_produto(self, prod: dict):
        itens = prod.get("items") or []
        if not itens:
            return None
        sku = itens[0]

        seller = self._melhor_seller(sku.get("sellers") or [])
        if seller is None:
            return None
        oferta = seller.get("commertialOffer") or {}
        preco = oferta.get("Price")
        if not preco or preco <= 0:
            return None
        preco = float(preco)

        imagem = None
        imagens = sku.get("images") or []
        if imagens:
            imagem = imagens[0].get("imageUrl")

        nome = prod.get("productName") or sku.get("name")
        unidade = sku.get("measurementUnit")
        link = prod.get("link")  # URL completa da página do produto (VTEX)

        # Desconto: ListPrice (preço "de") vs Price (preço atual)
        lista = oferta.get("ListPrice")
        preco_de = desconto_pct = None
        if lista and float(lista) > preco:
            preco_de = round(float(lista), 2)
            desconto_pct = round((preco_de - preco) / preco_de * 100)
            if desconto_pct <= 0:
                preco_de = desconto_pct = None

        ean = (sku.get("ean") or "").strip()
        if not ean_valido(ean):
            # Sem código de barras real: se for FRESCO (açougue/hortifrúti/padaria),
            # vira perecível (cruza por nome); senão é descartado (fora do escopo).
            cod = str(prod.get("productId") or sku.get("itemId") or prod.get("linkText") or "").strip()
            return perecivel_se_fresco(
                cod_interno=cod, nome=nome, preco=preco, unidade=unidade,
                imagem_url=imagem, url=link, preco_de=preco_de, desconto_pct=desconto_pct,
            )

        return ItemColeta(
            ean=ean,
            nome=nome,
            marca=prod.get("brand"),
            unidade=unidade,
            qtd_embalagem=None,  # gramatura extraída na normalização (Fase 2)
            imagem_url=imagem,
            preco=preco,
            preco_lista=lista,
            url=link,
            preco_de=preco_de,
            desconto_pct=desconto_pct,
        )

    @staticmethod
    def _melhor_seller(sellers: list[dict]) -> dict | None:
        """Primeiro seller disponível; se nenhum marcar disponibilidade, o primeiro."""
        for s in sellers:
            oferta = s.get("commertialOffer") or {}
            if oferta.get("IsAvailable") and (oferta.get("AvailableQuantity") or 0) > 0:
                return s
        return sellers[0] if sellers else None

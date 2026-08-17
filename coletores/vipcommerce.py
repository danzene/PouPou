"""Coletor genérico VIP Commerce — reaproveitável entre mercados VIP Commerce
(Enxuto, Dalben e outros regionais do interior de SP).

A API fica em services.vipcommerce.com.br e é identificada por org + domainkey +
filial + centro de distribuição (CD). O catálogo exige um bearer token anônimo
(sem `exp`, longa duração) + os headers DomainKey/OrganizationId.

TOKEN DINÂMICO: o coletor gera o próprio token no endpoint público de login da loja
(`/auth/loja/login`), com as credenciais anônimas embutidas no front do VIP Commerce
(username "loja" + key da plataforma). Assim não precisa capturar token manualmente.
Se algum dia o login falhar, dá pra sobrescrever via env var (token_env).

Busca:  GET {SERVICES}/org/{org}/filial/{f}/centro_distribuicao/{cd}
             /loja/buscas/produtos/termo/{termo}?page=N
Campos: codigo_barras -> ean | descricao -> nome | preco | unidade_sigla | imagem | link
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Iterator
from urllib.parse import quote

import httpx

from .base import Coletor, ItemColeta, perecivel_se_fresco
from .vtex import ean_valido  # mesma validação de GTIN

log = logging.getLogger("coletor.vipcommerce")

SERVICES = "https://services.vipcommerce.com.br/api-admin/v1"
IMG_BASE = "https://produto-assets-vipcommerce-com-br.br-se1.magaluobjects.com/250x250"
MAX_PAGINAS = 5  # teto de páginas por termo (coleta gentil)

# Credenciais anônimas do "usuário de loja" do VIP Commerce (embutidas no JS público
# do front — mesmas que o navegador anônimo usa). Sobrescrevíveis por env se rotacionarem.
VIP_LOJA_USER = os.getenv("VIP_LOJA_USER", "loja")
VIP_LOJA_KEY = os.getenv("VIP_LOJA_KEY", "df072f85df9bf7dd71b6811c34bdbaa4f219d98775b56cff9dfa5f8ca1bf8469")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 MercadoBaratoBot/0.1")
_RE_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")


def mintar_token(org: str, domainkey: str, *, api_base: str = SERVICES,
                 timeout: float = 20.0, contato: str = "") -> str | None:
    """Gera um bearer token anônimo via login público da loja. Devolve o JWT ou None."""
    url = f"{api_base}/org/{org}/auth/loja/login"
    headers = {"User-Agent": _UA, "Accept": "application/json", "Content-Type": "application/json",
               "Origin": f"https://www.{domainkey}", "Referer": f"https://www.{domainkey}/",
               "DomainKey": domainkey, "OrganizationId": str(org)}
    if contato:
        headers["From"] = contato
    body = {"domain": domainkey, "username": VIP_LOJA_USER, "key": VIP_LOJA_KEY}
    try:
        r = httpx.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.HTTPError as exc:
        log.warning("login VIP (%s) falhou: %s", domainkey, exc)
        return None
    if r.status_code != 200:
        log.warning("login VIP (%s) devolveu %s: %s", domainkey, r.status_code, r.text[:120])
        return None
    m = _RE_JWT.search(r.text)
    return m.group(0) if m else None


class ColetorVIPCommerce(Coletor):
    plataforma = "vipcommerce"

    def __init__(
        self,
        *,
        slug: str,
        nome: str,
        url_base: str,
        org: str,
        domainkey: str,
        filial: int = 1,
        cd: int = 1,
        token_env: str = "",
        api_base: str = SERVICES,   # algumas orgs usam host -beta (ex.: Spani)
        cidade: str | None = None,
        delay: float = 1.2,
        timeout: float = 20.0,
        max_por_termo: int = 50,
        contato: str = "contato@exemplo.com",
        **_ignorado,
    ) -> None:
        self.slug = slug
        self.nome = nome
        self.url_base = url_base.rstrip("/")
        self.org = str(org)
        self.domainkey = domainkey
        self.filial = filial
        self.cd = cd
        self.api_base = api_base
        # token do .env é só OVERRIDE opcional; o padrão é gerar dinamicamente.
        self._token_override = os.getenv(token_env, "").strip() if token_env else ""
        self.cidade = cidade
        self.delay = max(delay, 0.0)
        self.timeout = timeout
        self.max_por_termo = max_por_termo
        self.contato = contato
        self._token = ""
        self._ja_renovou = False

    @property
    def _base_loja(self) -> str:
        return f"{self.api_base}/org/{self.org}/filial/{self.filial}/centro_distribuicao/{self.cd}/loja"

    def _headers(self) -> dict:
        return {
            "User-Agent": _UA, "Accept": "application/json", "Accept-Language": "pt-BR,pt;q=0.9",
            "Origin": self.url_base, "Referer": self.url_base + "/",
            "DomainKey": self.domainkey, "OrganizationId": self.org,
            "Authorization": f"Bearer {self._token}", "From": self.contato,
        }

    def _renovar(self, client: httpx.Client) -> bool:
        """Gera um token novo e atualiza o client. Não renova se há override manual."""
        if self._token_override:
            return False
        novo = mintar_token(self.org, self.domainkey, api_base=self.api_base, timeout=self.timeout, contato=self.contato)
        if novo:
            self._token = novo
            client.headers["Authorization"] = f"Bearer {novo}"
            log.info("[%s] token VIP renovado.", self.slug)
            return True
        return False

    def coletar(self, sementes: list[tuple[str, str]]) -> Iterator[ItemColeta]:
        self._token = self._token_override or mintar_token(
            self.org, self.domainkey, api_base=self.api_base, timeout=self.timeout, contato=self.contato) or ""
        if not self._token:
            log.warning("[%s] não obtive token VIP (login falhou) — pulando mercado.", self.slug)
            return
        self._ja_renovou = False
        with httpx.Client(headers=self._headers(), timeout=self.timeout, follow_redirects=True) as client:
            for termo, categoria in sementes:
                yield from self._buscar_termo(client, termo, categoria)

    # -- varredura de CATÁLOGO COMPLETO (por árvore de departamentos) ------
    def coletar_catalogo(self) -> Iterator[ItemColeta]:
        """Varre TODO o catálogo pela árvore de departamentos (não por termos-semente).

        Só os departamentos RAIZ (parent_id=None) aceitam listagem de produtos; cada um
        já agrega todas as suas seções. Pagina com ?page=N&limit=500. Dedup por produto_id.
        """
        from .mapa_categorias import mapear_categoria

        self._token = self._token_override or mintar_token(
            self.org, self.domainkey, api_base=self.api_base, timeout=self.timeout, contato=self.contato) or ""
        if not self._token:
            log.warning("[%s] não obtive token VIP — pulando mercado.", self.slug)
            return
        self._ja_renovou = False
        with httpx.Client(headers=self._headers(), timeout=self.timeout, follow_redirects=True) as client:
            r = self._get(client, f"{self._base_loja}/classificacoes_mercadologicas/departamentos/arvore")
            if r is None or r.status_code not in (200, 206):
                log.warning("[%s] não obtive a árvore de departamentos.", self.slug)
                return
            try:
                dados = r.json().get("data") or []
            except ValueError:
                return
            raizes = [d for d in dados if d.get("parent_id") is None]
            vistos: set = set()
            for dep in raizes:
                cid = dep.get("classificacao_mercadologica_id")
                categoria = mapear_categoria(dep.get("descricao"))
                yield from self._varrer_departamento(client, cid, categoria, vistos)

    def _varrer_departamento(self, client, cid, categoria, vistos) -> Iterator[ItemColeta]:
        page = 1
        while True:
            url = (f"{self._base_loja}/classificacoes_mercadologicas/departamentos/"
                   f"{cid}/produtos?page={page}&limit=500")
            r = self._get(client, url)
            if r is None or r.status_code not in (200, 206):
                return
            try:
                j = r.json()
            except ValueError:
                return
            data = j.get("data") or []
            if not data:
                return
            for prod in data:
                pid = prod.get("produto_id")
                if pid in vistos:
                    continue
                vistos.add(pid)
                item = self._parse(prod, categoria)
                if item is not None:
                    yield item
            pag = j.get("paginator") or {}
            if page >= (pag.get("total_pages") or 1):
                return
            page += 1

    def _get(self, client: httpx.Client, url: str, tentativas: int = 4) -> httpx.Response | None:
        """GET com pacing, renovação de token em 401 e retry+backoff em 429/5xx
        (auto-cura rate-limit; None se esgotar — o mercado não é derrubado)."""
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
            if resp.status_code == 401 and not self._ja_renovou and self._renovar(client):
                self._ja_renovou = True
                continue  # refaz a requisição já com token novo
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if i == tentativas - 1:
                    log.warning("[%s] HTTP %s após %d tentativas", self.slug, resp.status_code, tentativas)
                    return resp
                ra = resp.headers.get("retry-after")
                pausa = float(ra) if (ra and ra.replace(".", "", 1).isdigit()) else espera
                time.sleep(min(pausa, 30)); espera = min(espera * 2, 30); continue
            return resp
        return resp

    def _buscar_termo(self, client: httpx.Client, termo: str, categoria: str | None) -> Iterator[ItemColeta]:
        vistos = 0
        for page in range(1, MAX_PAGINAS + 1):
            if vistos >= self.max_por_termo:
                return
            url = f"{self._base_loja}/buscas/produtos/termo/{quote(termo, safe='')}?page={page}"
            resp = self._get(client, url)
            if resp is None:
                return
            if resp.status_code == 401:
                log.warning("[%s] 401 sem renovar — verifique as credenciais VIP.", self.slug)
                return
            if resp.status_code not in (200, 206):
                log.warning("[%s] '%s' p%d -> %s; parando termo.", self.slug, termo, page, resp.status_code)
                return
            try:
                payload = resp.json()
            except ValueError:
                return
            produtos = self._extrair_lista(payload)
            if not produtos:
                return
            for prod in produtos:
                item = self._parse(prod, categoria)
                if item is not None:
                    vistos += 1
                    yield item

    @staticmethod
    def _extrair_lista(payload) -> list:
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(data, dict):
            return data.get("produtos") or data.get("items") or []
        return data if isinstance(data, list) else []

    def _parse(self, prod: dict, categoria: str | None):
        preco = _num(prod.get("preco"))
        if preco is None or preco <= 0:
            return None
        if prod.get("disponivel") is False:
            return None
        img = prod.get("imagem")
        imagem_url = f"{IMG_BASE}/{img}" if img else None
        # URL canônica do VIP Commerce: /produto/{produto_id}/{slug} (resolve pelo id,
        # robusto a mudança de slug). Fallback: slug puro; ou a própria URL se já vier completa.
        link = prod.get("link")
        pid = prod.get("produto_id")
        if link and not link.startswith("http"):
            slug = link.lstrip("/")
            link = f"{self.url_base}/produto/{pid}/{slug}" if pid else f"{self.url_base}/{slug}"
        nome = prod.get("descricao")
        sigla = (prod.get("unidade_sigla") or "").strip().upper()
        orig = _num(prod.get("preco_original"))
        peso = _num(prod.get("quantidade_unidade_diferente"))
        # Item vendido A PESO no VIP: unidade 'KG' + tem "unidade diferente" (o peso da peça).
        por_peso = bool(prod.get("possui_unidade_diferente")) and sigla == "KG" and peso and peso > 0
        # OFERTA ATIVA (ex.: "Ofertas fidelidade"): é o preço que o site exibe EM DESTAQUE.
        # Pra não ficar diferente do site, mostramos o preço da oferta e deixamos o normal como "de".
        of = prod.get("oferta") if prod.get("em_oferta") else None

        if por_peso:
            # R$/kg NORMAL = preco_original (= preco/peso). Se há oferta, o site mostra o R$/kg DA OFERTA.
            reg = orig if (orig and orig > 0) else (round(preco / peso, 2) if peso else preco)
            cur = _num(of.get("preco_original")) if of else None
            preco_final = cur if (cur and cur > 0) else reg
            unidade = "kg"
        else:
            # Vendido por UNIDADE/pacote: preço é o TOTAL. Oferta = preco_oferta; normal = preco_antigo.
            cur = _num(of.get("preco_oferta")) if of else None
            if cur and cur > 0:
                preco_final = cur
                reg = _num(of.get("preco_antigo")) or preco
            else:
                preco_final = preco
                reg = orig if (orig and orig > preco) else None
            unidade = (sigla.lower() or None)

        # "de" = preço NORMAL, quando maior que o atual (mostra o desconto igual o site)
        preco_de = desconto_pct = None
        if reg and preco_final and reg > preco_final + 0.005:
            preco_de = round(reg, 2)
            desconto_pct = round((reg - preco_final) / reg * 100) or None
            if not desconto_pct:
                preco_de = None
        if not preco_final or preco_final <= 0:
            return None
        # TIPO/RÓTULO da oferta. Se o preço exige um PROGRAMA (clube/fidelidade/cartão),
        # oferta_tipo guarda o NOME do programa (ex.: "Viva", "Fidelidade") pra virar um
        # selo na tela; senão é "oferta" (promoção pra todos, sem cartão). Sinais do VIP:
        # nome/tag/classe com "fidelidade" (Ofertas fidelidade) ou "vip" (preco-vip = Viva).
        oferta_tipo = None
        if of and preco_de:
            nome_of = (of.get("nome") or "").strip()
            blob = f"{of.get('classe_oferta') or ''} {of.get('tag') or ''} {nome_of}".lower()
            if any(t in blob for t in ("fidelidade", "vip", "clube", "cliente", "socio", "sócio")):
                rot = re.sub(r"(?i)^ofertas?\s+", "", nome_of).strip() or nome_of
                oferta_tipo = (rot[:1].upper() + rot[1:])[:20]  # "Viva", "Fidelidade"
            else:
                oferta_tipo = "oferta"  # promoção pra todos (sem cartão)

        ean = str(prod.get("codigo_barras") or "").strip()
        if not ean_valido(ean):
            # Sem código de barras: se for fresco (açougue/hortifrúti/...), vira perecível.
            return perecivel_se_fresco(
                cod_interno=pid, nome=nome, preco=preco_final, unidade=unidade,
                imagem_url=imagem_url, url=link, preco_de=preco_de, desconto_pct=desconto_pct,
                oferta_tipo=oferta_tipo,
            )
        return ItemColeta(
            ean=ean,
            nome=nome,
            marca=None,  # VIP dá marca_id numérico; nome de marca exigiria lookup (fase futura)
            unidade=unidade,
            qtd_embalagem=None,     # normalização extrai do nome
            imagem_url=imagem_url,
            preco=preco_final,
            preco_lista=preco_de,
            categoria=categoria,
            url=link,
            preco_de=preco_de,
            desconto_pct=desconto_pct,
            oferta_tipo=oferta_tipo,
        )


def _num(v) -> float | None:
    """Coage preço para float; VIP Commerce devolve como string ('28.89')."""
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "."))
    except (ValueError, TypeError):
        return None

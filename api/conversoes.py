"""Avisos de conversão do SERVIDOR para o Meta (API de Conversões) e o Google (GA4 Measurement
Protocol) — Fase A3.

Regras:
- Só é chamado DEPOIS do commit e só para quem aceitou os cookies (quem decide é api/main.py).
- Sem o segredo na variável de ambiente (META_CAPI_TOKEN / GA4_API_SECRET), não envia nada.
- NUNCA levanta erro: qualquer falha vira um aviso no log e o cadastro/teste/pagamento segue.
- Sem e-mail, nome ou telefone: só o id da conta embaralhado (sha256), os códigos do navegador
  (fbp/fbc), o IP e o navegador (correspondência avançada desligada).
"""
from __future__ import annotations

import hashlib
import logging
import os
import time

import httpx

_LOG = logging.getLogger("api")
_HTTP = httpx.Client(timeout=3)
META_VERSAO = "v21.0"
META_PIXEL_PADRAO = "1624321239375976"
GA4_PADRAO = "G-29JELR1XKT"
URL_SITE = "https://poupouapp.com.br/"


def _limpa(msg: str, *segredos: str) -> str:
    """Tira os segredos de uma mensagem de erro antes de ir pro log."""
    for s in segredos + (os.getenv("META_CAPI_TOKEN", "").strip(), os.getenv("GA4_API_SECRET", "").strip()):
        if s:
            msg = msg.replace(s, "***")
    return msg[:300]


def external_id(uid) -> str | None:
    """Id da conta embaralhado (sha256 em hexadecimal), como o Meta pede."""
    return hashlib.sha256(str(uid).encode("utf-8")).hexdigest() if uid else None


def meta_ligado() -> bool:
    return bool(os.getenv("META_CAPI_TOKEN", "").strip())


def ga4_ligado() -> bool:
    return bool(os.getenv("GA4_API_SECRET", "").strip())


def enviar_meta(evento: str, event_id: str, quando_ts=None, user_data: dict | None = None,
                custom_data: dict | None = None, timeout: float | None = None) -> bool:
    """1 evento para a API de Conversões do Meta. True = o Meta aceitou. Nunca levanta erro.
    timeout: segundos (menor no cadastro/teste/pagamento, pra não atrasar a pessoa); sem ele, 3 s."""
    token = os.getenv("META_CAPI_TOKEN", "").strip()
    if not token:
        return False
    pixel = os.getenv("META_PIXEL_ID", "").strip() or META_PIXEL_PADRAO
    try:
        ud = {k: v for k, v in (user_data or {}).items() if v not in (None, "")}
        ev = {"event_name": evento, "event_time": int(quando_ts or time.time()), "event_id": event_id,
              "action_source": "website", "event_source_url": URL_SITE, "user_data": ud}
        cd = {k: v for k, v in (custom_data or {}).items() if v is not None}
        if cd:
            ev["custom_data"] = cd
        corpo = {"data": [ev], "access_token": token}   # token no corpo: a URL não leva segredo
        teste = os.getenv("META_TEST_EVENT_CODE", "").strip()
        if teste:
            corpo["test_event_code"] = teste
        r = _HTTP.post(f"https://graph.facebook.com/{META_VERSAO}/{pixel}/events", json=corpo,
                       timeout=timeout if timeout else httpx.USE_CLIENT_DEFAULT)
        if 200 <= r.status_code < 300:
            return True
        _LOG.warning("conversoes meta %s: HTTP %s %s", evento, r.status_code, _limpa(r.text, token))
    except Exception as e:  # noqa: BLE001
        _LOG.warning("conversoes meta %s: %s %s", evento, type(e).__name__, _limpa(str(e), token))
    return False


def enviar_ga4(client_id: str, session_id, eventos: list) -> bool:
    """Eventos para o GA4 (Measurement Protocol). Sem client_id não envia. Nunca levanta erro."""
    segredo = os.getenv("GA4_API_SECRET", "").strip()
    if not segredo or not client_id or not eventos:
        return False
    mid = os.getenv("GA4_MEASUREMENT_ID", "").strip() or GA4_PADRAO
    try:
        evs = []
        for e in eventos:
            params = dict(e.get("params") or {})
            if session_id:
                params.setdefault("session_id", str(session_id))
            params.setdefault("engagement_time_msec", 1)   # sem isso o GA4 não liga o evento à sessão
            evs.append({"name": e["name"], "params": params})
        r = _HTTP.post("https://www.google-analytics.com/mp/collect",
                       params={"measurement_id": mid, "api_secret": segredo},
                       json={"client_id": str(client_id), "events": evs})
        if 200 <= r.status_code < 300:
            return True
        _LOG.warning("conversoes ga4: HTTP %s", r.status_code)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("conversoes ga4: %s %s", type(e).__name__, _limpa(str(e), segredo))
    return False

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException

from app.core.config import settings


def mp_request(method: str, url: str, token: str, body: dict | None = None, headers: dict | None = None):
    req_headers = {
        "Content-Type": "application/json",
    }
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    if headers:
        req_headers.update(headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        raise HTTPException(
            status_code=502,
            detail=f"Falha Mercado Pago ({exc.code}): {error_body or 'erro sem detalhe'}",
        ) from exc


def get_mp_user_id(access_token: str) -> str:
    data = mp_request("GET", "https://api.mercadopago.com/users/me", access_token)
    user_id = data.get("id")
    if not user_id:
        raise HTTPException(status_code=502, detail="Mercado Pago nao retornou user_id")
    return str(user_id)


def exchange_oauth_code(code: str, redirect_uri: str) -> dict:
    if not settings.MP_APP_ID or not settings.MP_CLIENT_SECRET or not redirect_uri:
        raise HTTPException(
            status_code=500,
            detail="Configure MP_APP_ID, MP_CLIENT_SECRET e MP_OAUTH_REDIRECT_URI para vincular contas Mercado Pago",
        )
    body = {
        "client_id": settings.MP_APP_ID,
        "client_secret": settings.MP_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "test_token": "false",
    }
    return mp_request("POST", "https://api.mercadopago.com/oauth/token", "", body=body)


def normalize_external_id(value: str, max_length: int = 39) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", "", value.upper())
    return (normalized or f"CP{int(time.time())}")[:max_length]


def create_default_store(cliente) -> dict:
    access_token = (cliente.mp_access_token or "").strip()
    if not access_token:
        raise HTTPException(status_code=422, detail="Cliente sem MP_ACCESS_TOKEN cadastrado")

    user_id = (cliente.mp_user_id or "").strip() or get_mp_user_id(access_token)
    external_id = (cliente.mp_store_external_id or "").strip() or normalize_external_id(
        f"CPSTORE{cliente.id}",
        max_length=60,
    )
    body = {
        "name": (cliente.nome_empresa or "CompactPay")[:45],
        "external_id": external_id,
        "location": {
            "street_number": settings.MP_DEFAULT_STORE_STREET_NUMBER,
            "street_name": settings.MP_DEFAULT_STORE_STREET_NAME,
            "city_name": settings.MP_DEFAULT_STORE_CITY_NAME,
            "state_name": settings.MP_DEFAULT_STORE_STATE_NAME,
            "latitude": settings.MP_DEFAULT_STORE_LATITUDE,
            "longitude": settings.MP_DEFAULT_STORE_LONGITUDE,
            "reference": cliente.nome_empresa or "CompactPay",
        },
    }
    store = mp_request(
        "POST",
        f"https://api.mercadopago.com/users/{urllib.parse.quote(user_id)}/stores",
        access_token,
        body=body,
    )
    return {
        "mp_user_id": user_id,
        "mp_store_id": str(store.get("id") or ""),
        "mp_store_external_id": store.get("external_id") or external_id,
    }


def ensure_cliente_store(cliente) -> None:
    if cliente.mp_store_id and cliente.mp_store_external_id:
        if not cliente.mp_user_id and cliente.mp_access_token:
            cliente.mp_user_id = get_mp_user_id(cliente.mp_access_token.strip())
        return
    store_data = create_default_store(cliente)
    cliente.mp_user_id = store_data["mp_user_id"]
    cliente.mp_store_id = store_data["mp_store_id"]
    cliente.mp_store_external_id = store_data["mp_store_external_id"]


def create_pos_for_machine(cliente, maquina) -> dict:
    access_token = (cliente.mp_access_token or "").strip()
    if not access_token:
        raise HTTPException(status_code=422, detail="Cliente sem MP_ACCESS_TOKEN cadastrado")
    ensure_cliente_store(cliente)

    external_id = normalize_external_id(maquina.id_hardware, max_length=39)
    body = {
        "name": (maquina.nome_local or maquina.id_hardware)[:44],
        "fixed_amount": False,
        "store_id": int(cliente.mp_store_id) if str(cliente.mp_store_id or "").isdigit() else cliente.mp_store_id,
        "external_store_id": cliente.mp_store_external_id,
        "external_id": external_id,
        "category": settings.MP_DEFAULT_POS_CATEGORY,
    }
    pos = mp_request(
        "POST",
        "https://api.mercadopago.com/pos",
        access_token,
        body=body,
        headers={"X-Idempotency-Key": f"{external_id}-{int(time.time() * 1000)}"},
    )
    return {
        "mp_pos_id": str(pos.get("id") or ""),
        "mp_pos_external_id": pos.get("external_id") or external_id,
        "mp_qr_image": ((pos.get("qr") or {}).get("image") or ""),
    }

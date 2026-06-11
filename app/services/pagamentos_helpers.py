from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import Cliente
from app.services.mercado_pago import mp_request


def calcular_pulsos_por_valor(valor: float) -> int:
    # Regra atual: 1 pulso por R$1, minimo de 1 pulso para qualquer valor positivo.
    quantia = Decimal(str(valor))
    if quantia <= 0:
        return 1
    pulsos = int(quantia)
    return max(1, pulsos)


def iter_mp_tokens(db: Session):
    seen = set()
    if settings.MP_ACCESS_TOKEN:
        seen.add(settings.MP_ACCESS_TOKEN)
        yield settings.MP_ACCESS_TOKEN
    for token in db.query(Cliente.mp_access_token).filter(Cliente.mp_access_token.isnot(None)).all():
        value = (token[0] or "").strip()
        if value and value not in seen:
            seen.add(value)
            yield value


def mp_request_with_known_tokens(db: Session, method: str, url: str, preferred_token: str | None = None):
    errors = []
    tokens = []
    if preferred_token:
        tokens.append(preferred_token)
    tokens.extend(list(iter_mp_tokens(db)))
    for token in tokens:
        try:
            return mp_request(method, url, token), token
        except HTTPException as exc:
            errors.append(str(exc.detail))
    raise HTTPException(
        status_code=502,
        detail="Nao foi possivel consultar o Mercado Pago com as credenciais cadastradas: " + " | ".join(errors[-3:]),
    )


def parse_machine_id_from_external_reference(external_reference: str | None) -> str | None:
    if not external_reference:
        return None
    # Formato esperado: MACHINE_ID:timestamp
    if ":" in external_reference:
        return external_reference.split(":", 1)[0].strip() or None
    return external_reference.strip() or None


def extract_terminal_id(payload: dict) -> str | None:
    candidates = [
        ((payload.get("point_of_interaction") or {}).get("transaction_data") or {}).get("terminal_id"),
        ((payload.get("point_of_interaction") or {}).get("transaction_data") or {}).get("device_id"),
        (payload.get("metadata") or {}).get("terminal_id"),
        payload.get("terminal_id"),
    ]
    for candidate in candidates:
        if candidate:
            return str(candidate).strip()
    return None


def payment_metadata(payment_data: dict) -> dict:
    issuer = payment_data.get("issuer") or {}
    card = payment_data.get("card") or {}
    return {
        "provider": "mercado_pago",
        "provider_payment_id": str(payment_data.get("id") or "").strip() or None,
        "payment_type": payment_data.get("payment_type_id") or payment_data.get("payment_method_id"),
        "card_brand": payment_data.get("payment_method_id") or card.get("cardholder", {}).get("name"),
        "bank_name": issuer.get("name") or issuer.get("id"),
    }

import re
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import Cliente, HistoricoOperacao, VendaPagamento
from app.services.mercado_pago import mp_request

NON_RELEASED_PULSE_STATUSES = {
    "falha",
    "falha_timeout",
    "falha_publicacao",
    "falha_cmd_ignorado",
    "falha_bloqueado",
    "falha_sem_confirmacao",
    "saldo_pendente",
    "pulso_sem_retorno",
}

MERCADO_PAGO_REFUND_PROVIDERS = {"", "mercado_pago", "manual"}


def calcular_pulsos_por_valor(valor: float) -> int:
    # Regra atual: 1 pulso por R$1, minimo de 1 pulso para qualquer valor positivo.
    quantia = Decimal(str(valor))
    if quantia <= 0:
        return 1
    pulsos = int(quantia)
    return max(1, pulsos)


def should_auto_refund_on_pulse_failure(pulse_status: str | None) -> bool:
    normalized = str(pulse_status or "").strip().lower()
    return normalized in NON_RELEASED_PULSE_STATUSES


def should_allow_refund(pulse_status: str | None, refunded_at, provider_payment_id: str | None, provider: str | None) -> bool:
    if refunded_at:
        return False
    if not provider_payment_id:
        return False
    normalized_provider = str(provider or "").strip().lower()
    return normalized_provider in MERCADO_PAGO_REFUND_PROVIDERS


def should_use_mercado_pago_refund(historico: HistoricoOperacao | None) -> bool:
    if not historico:
        return False
    normalized_provider = str(historico.provider or "").strip().lower()
    return normalized_provider in MERCADO_PAGO_REFUND_PROVIDERS


def extract_provider_payment_id(historico: HistoricoOperacao | None) -> str | None:
    if not historico:
        return None
    if historico.provider_payment_id:
        return historico.provider_payment_id
    match = re.search(r"(?:payment_id|mp_order_id)=([^,\)\s]+)", historico.descricao or "")
    return match.group(1) if match else None


def auto_refund_failed_pulse(db: Session, historico: HistoricoOperacao | None, maquina=None) -> bool:
    if not historico or historico.refunded_at:
        return False
    if not should_auto_refund_on_pulse_failure(getattr(historico, "pulse_status", None)):
        return False
    if not should_use_mercado_pago_refund(historico):
        return False

    payment_id = extract_provider_payment_id(historico)
    if not payment_id:
        return False

    token = ""
    if maquina is not None:
        token = (getattr(maquina, "dono", None).mp_access_token if getattr(maquina, "dono", None) else "") or ""
    if not token:
        return False

    mp_request(
        "POST",
        f"https://api.mercadopago.com/v1/payments/{payment_id}/refunds",
        token.strip(),
        body={},
        headers={"X-Idempotency-Key": f"refund-{payment_id}-{historico.id}"},
    )
    refunded_at = datetime.utcnow()
    historico.refunded_at = refunded_at
    venda = db.query(VendaPagamento).filter(VendaPagamento.historico_id == historico.id).first()
    if venda:
        venda.refunded_at = refunded_at
    db.add(historico)
    if venda:
        db.add(venda)
    db.commit()
    return True


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


def _normalize_mp_identifier(value) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _collect_values_by_key(payload, keys: set[str]) -> set[str]:
    found = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized_key = str(key).lower()
            if normalized_key in keys:
                normalized_value = _normalize_mp_identifier(value)
                if normalized_value:
                    found.add(normalized_value)
            found.update(_collect_values_by_key(value, keys))
    elif isinstance(payload, list):
        for item in payload:
            found.update(_collect_values_by_key(item, keys))
    return found


def extract_mp_location_ids(payload: dict) -> dict[str, set[str]]:
    return {
        "store_ids": _collect_values_by_key(payload, {"store_id", "storeid", "loja_id", "loja"}),
        "store_external_ids": _collect_values_by_key(
            payload,
            {"external_store_id", "store_external_id", "external_storeid", "mp_store_external_id"},
        ),
        "pos_ids": _collect_values_by_key(payload, {"pos_id", "posid", "point_id", "caixa_id", "caixa"}),
        "pos_external_ids": _collect_values_by_key(
            payload,
            {"external_pos_id", "pos_external_id", "external_posid", "mp_pos_external_id"},
        ),
    }


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

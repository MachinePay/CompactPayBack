import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import Cliente, HistoricoOperacao, VendaPagamento
from app.services.mercado_pago import mp_request

NON_RELEASED_PULSE_STATUSES = {
    "falha_publicacao",
    "falha_cmd_ignorado",
    "falha_bloqueado",
    "saldo_pendente",
    # A placa nunca respondeu nada (nem CMD_RECEBIDO) depois de todas as
    # tentativas de reenvio via MQTT - diferente dos status "ambiguos" abaixo,
    # aqui da' pra ter certeza que o pulso fisico nunca chegou a acontecer
    # (o codigo que aciona o rele so roda depois de CMD_RECEBIDO), entao e'
    # seguro estornar sozinho.
    "falha_dispositivo_offline",
}

# Status que so indicam falta de confirmacao (o comando pode ter sido enviado
# e ate executado normalmente, ex.: maquina sem contador para confirmar o
# pulso). Nao entram no extorno automatico - ficam so para revisao/extorno
# manual do operador.
AMBIGUOUS_PULSE_STATUSES = {
    "falha",
    "falha_timeout",
    "falha_sem_confirmacao",
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


INVALID_TERMINAL_ID_VALUES = {"n/a", "na", "null", "none", "undefined", "-"}


def extract_terminal_id(payload: dict) -> str | None:
    candidates = [
        ((payload.get("point_of_interaction") or {}).get("transaction_data") or {}).get("terminal_id"),
        ((payload.get("point_of_interaction") or {}).get("transaction_data") or {}).get("device_id"),
        (payload.get("metadata") or {}).get("terminal_id"),
        payload.get("terminal_id"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        normalized = str(candidate).strip()
        # Pix via QR code (sem maquininha fisica) vem do Mercado Pago com um
        # placeholder tipo "N/A" nesse campo em vez de vir vazio - sem esse
        # filtro, esse texto era aceito como se fosse um terminal_id de
        # verdade e ficava salvo/exibido no lugar do ID da maquininha.
        if normalized.lower() in INVALID_TERMINAL_ID_VALUES:
            continue
        return normalized
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


# Cache em memoria (payment_method_id, issuer_id) -> nome do banco. A lista
# de emissores por bandeira e' cadastro estatico do Mercado Pago (nao muda a
# cada pagamento), entao nao vale a pena consultar a API de novo pra cada
# transacao do mesmo banco/bandeira. Reseta a cada deploy/restart, sem
# problema - so reenche sozinho na proxima vez que aparecer.
_CARD_ISSUER_NAME_CACHE: dict[tuple[str, str], str] = {}


def resolve_card_issuer_name(payment_method_id: str | None, issuer_id, token: str | None) -> str | None:
    """O pagamento em si so traz o issuer_id (um numero) quando veio de
    maquininha fisica, nao o nome do banco - precisa de uma segunda consulta
    na lista de emissores daquela bandeira pra resolver o nome. Nunca deixa
    essa consulta extra derrubar o processamento do pagamento: qualquer falha
    aqui so significa que o banco fica sem nome, nada mais."""
    if not payment_method_id or issuer_id is None or not token:
        return None
    # A lista de emissores do Mercado Pago e' organizada por bandeira/rede, nao
    # por variante debito/credito - "debvisa"/"debmaster" (o payment_method_id
    # que a maquininha manda pra debito) da 404 nessa consulta (confirmado em
    # producao); a bandeira base ("visa"/"master") sim.
    lookup_payment_method_id = (
        payment_method_id[len("deb"):] if payment_method_id.startswith("deb") else payment_method_id
    )
    issuer_id_str = str(issuer_id)
    cache_key = (lookup_payment_method_id, issuer_id_str)
    if cache_key in _CARD_ISSUER_NAME_CACHE:
        return _CARD_ISSUER_NAME_CACHE[cache_key]
    try:
        issuers = mp_request(
            "GET",
            f"https://api.mercadopago.com/v1/payment_methods/card_issuers?payment_method_id={lookup_payment_method_id}",
            token,
        )
    except Exception:
        return None
    if not isinstance(issuers, list):
        return None
    name = next((item.get("name") for item in issuers if str(item.get("id")) == issuer_id_str), None)
    if name:
        _CARD_ISSUER_NAME_CACHE[cache_key] = name
    return name


# Nomes genericos que o catalogo de emissores do Mercado Pago devolve quando
# ele mesmo nao sabe identificar o banco (confirmado em producao: um cartao
# Santander de verdade voltou "Outro") - nesses casos vale tentar a segunda
# fonte (BIN) em vez de aceitar um nome inutil.
_UNHELPFUL_BANK_NAMES = {"outro", "outros", "other", "others", "otro", "otros bancos"}

_BIN_BANK_NAME_CACHE: dict[str, str] = {}


def resolve_bank_name_from_bin(bin_number) -> str | None:
    """O BIN (6-8 primeiros digitos do cartao, que o Mercado Pago ja manda em
    card.first_six_digits) identifica o banco emissor de forma independente
    do catalogo do Mercado Pago - usa a API publica/gratuita binlist.net
    como segunda fonte quando o Mercado Pago nao sabe o banco. Sem chave de
    API, cacheado em memoria por BIN. Qualquer falha aqui so deixa o banco
    sem nome, nunca derruba o processamento do pagamento."""
    if not bin_number:
        return None
    bin_str = str(bin_number)[:8]
    if bin_str in _BIN_BANK_NAME_CACHE:
        return _BIN_BANK_NAME_CACHE[bin_str]
    try:
        request = urllib.request.Request(
            f"https://lookup.binlist.net/{bin_str}",
            headers={"Accept-Version": "3"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    name = ((data.get("bank") or {}).get("name") or "").strip()
    if name:
        _BIN_BANK_NAME_CACHE[bin_str] = name
    return name or None


def payment_metadata(payment_data: dict, token: str | None = None) -> dict:
    issuer = payment_data.get("issuer") or {}
    card = payment_data.get("card") or {}
    bank_name = issuer.get("name")
    if not bank_name:
        issuer_id = payment_data.get("issuer_id") or issuer.get("id")
        bank_name = resolve_card_issuer_name(payment_data.get("payment_method_id"), issuer_id, token)
    if not bank_name or bank_name.strip().lower() in _UNHELPFUL_BANK_NAMES:
        bank_name = resolve_bank_name_from_bin(card.get("first_six_digits") or card.get("bin")) or bank_name
    return {
        "provider": "mercado_pago",
        "provider_payment_id": str(payment_data.get("id") or "").strip() or None,
        "payment_type": payment_data.get("payment_type_id") or payment_data.get("payment_method_id"),
        "card_brand": payment_data.get("payment_method_id") or card.get("cardholder", {}).get("name"),
        "card_last_four": card.get("last_four_digits") or None,
        "bank_name": bank_name,
    }

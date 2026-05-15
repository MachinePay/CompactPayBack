from datetime import datetime
from decimal import Decimal
import json
import time
import urllib.request
import urllib.error

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import EventoTipo, HistoricoOperacao, Maquina, MetodoPagamento, Transacao
from app.models.produto import Produto
from app.schemas.pagamento import PagamentoCreate, PagamentoOut
from app.services.mqtt_commands import publish_machine_credit_pulses

router = APIRouter()
ACTIVE_TERMINAL_BINDINGS: dict[str, dict] = {}
PROCESSED_PAYMENT_IDS: set[str] = set()


def _calcular_pulsos_por_valor(valor: float) -> int:
    # Regra atual: 1 pulso por R$1, minimo de 1 pulso para qualquer valor positivo.
    quantia = Decimal(str(valor))
    if quantia <= 0:
        return 1
    pulsos = int(quantia)
    return max(1, pulsos)


def _mp_request(method: str, url: str, token: str, body: dict | None = None, headers: dict | None = None):
    req_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if headers:
        req_headers.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
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


def _parse_machine_id_from_external_reference(external_reference: str | None) -> str | None:
    if not external_reference:
        return None
    # Formato esperado: MACHINE_ID:timestamp
    if ":" in external_reference:
        return external_reference.split(":", 1)[0].strip() or None
    return external_reference.strip() or None


def _extract_terminal_id(payload: dict) -> str | None:
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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_maquina_visivel(db: Session, maquina_id: str, role: str, cliente_id):
    query = db.query(Maquina).filter(Maquina.id_hardware == maquina_id)
    if role != "admin":
        query = query.filter(Maquina.cliente_id == cliente_id)
    maquina = query.first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")
    return maquina


@router.post("/callback-mercado-pago")
async def processar_pix(request: Request, dados: dict | None = None):
    payload_query = dict(request.query_params or {})
    payload_body = dados or {}
    dados = {**payload_query, **payload_body}

    # Suporta payload simples antigo: {status, id_hardware, valor}
    if dados.get("status") == "approved" and dados.get("id_hardware"):
        id_hardware = dados.get("id_hardware")
        valor = float(dados.get("valor", 1.0))
        db = SessionLocal()
        try:
            nova_transacao = Transacao(
                maquina_id=id_hardware,
                tipo=EventoTipo.in_flux,
                metodo=MetodoPagamento.digital,
                valor=valor,
                data_hora=datetime.utcnow(),
            )
            db.add(nova_transacao)
            db.add(
                HistoricoOperacao(
                    maquina_id=id_hardware,
                    categoria="PAGAMENTO",
                    descricao="Pagamento aprovado via callback simplificado",
                    valor=valor,
                    created_at=nova_transacao.data_hora,
                )
            )
            db.commit()
        finally:
            db.close()

        pulsos = _calcular_pulsos_por_valor(valor)
        publish_machine_credit_pulses(id_hardware, pulses=pulsos, action="paid")
        return {"status": "sucesso", "detalhe": "Pagamento digital registrado", "pulsos": pulsos}

    # Fluxo real Mercado Pago Point/Webhook
    mp_token = settings.MP_ACCESS_TOKEN
    if not mp_token:
        return {"status": "erro", "detalhe": "MP_ACCESS_TOKEN nao configurado"}

    topic = dados.get("topic") or dados.get("type") or ""
    action = dados.get("action") or ""
    data = dados.get("data") or {}
    order_id = data.get("id") or dados.get("id") or dados.get("data.id")

    if not order_id:
        return {"status": "ignorado", "detalhe": "Webhook sem id de order/payment"}

    # Para webhook da nova API /v1/orders: type=order action=order.processed
    # Busca detalhes da order para obter external_reference e amount
    if topic == "order" or action.startswith("order.") or str(order_id).startswith("ORD"):
        order_data = _mp_request("GET", f"https://api.mercadopago.com/v1/orders/{order_id}", mp_token)
        order_status = (order_data.get("status") or "").lower()
        if order_status not in {"processed"} and action != "order.processed":
            return {"status": "ignorado", "detalhe": f"Order ainda nao aprovada ({order_status or action})"}

        external_reference = order_data.get("external_reference")
        machine_id = _parse_machine_id_from_external_reference(external_reference)
        if not machine_id:
            return {"status": "erro", "detalhe": "Nao foi possivel identificar machine_id no external_reference"}

        amount = 1.0
        payments = ((order_data.get("transactions") or {}).get("payments") or [])
        if payments:
            amount = float(payments[0].get("amount") or 1.0)

        db = SessionLocal()
        try:
            duplicado = (
                db.query(HistoricoOperacao)
                .filter(
                    HistoricoOperacao.maquina_id == machine_id,
                    HistoricoOperacao.categoria == "PAGAMENTO",
                    HistoricoOperacao.descricao.contains(f"mp_order_id={order_id}"),
                )
                .first()
            )
            if duplicado:
                return {"status": "ignorado", "detalhe": "Pagamento ja processado"}

            transacao = Transacao(
                maquina_id=machine_id,
                tipo=EventoTipo.in_flux,
                metodo=MetodoPagamento.digital,
                valor=amount,
                data_hora=datetime.utcnow(),
            )
            db.add(transacao)
            db.add(
                HistoricoOperacao(
                    maquina_id=machine_id,
                    categoria="PAGAMENTO",
                    descricao=f"Pagamento aprovado via maquininha MP (mp_order_id={order_id})",
                    valor=amount,
                    created_at=transacao.data_hora,
                )
            )
            db.commit()
        finally:
            db.close()

        pulsos = _calcular_pulsos_por_valor(amount)
        publish_machine_credit_pulses(machine_id, pulses=pulsos, action="paid")
        return {"status": "sucesso", "detalhe": "Pagamento aprovado e pulsos enviados", "pulsos": pulsos}

    # Webhook de pagamento direto na conta MP (ex.: pagamento feito na maquininha vinculada)
    is_payment_event = topic in {"payment"} or action.startswith("payment.")
    if is_payment_event:
        payment_id = str((dados.get("data") or {}).get("id") or dados.get("id") or dados.get("data.id") or "").strip()
        if not payment_id:
            return {"status": "ignorado", "detalhe": "Evento payment sem id"}
        if payment_id in PROCESSED_PAYMENT_IDS:
            return {"status": "ignorado", "detalhe": "Pagamento ja processado"}

        payment_data = _mp_request("GET", f"https://api.mercadopago.com/v1/payments/{payment_id}", mp_token)
        payment_status = (payment_data.get("status") or "").lower()
        if payment_status not in {"approved", "authorized"}:
            return {"status": "ignorado", "detalhe": f"Pagamento ainda nao aprovado ({payment_status})"}

        terminal_id = _extract_terminal_id(payment_data)
        binding = ACTIVE_TERMINAL_BINDINGS.get(terminal_id or "")
        if not binding:
            # Fallback: se so existir uma escuta ativa, usa ela.
            if len(ACTIVE_TERMINAL_BINDINGS) == 1:
                binding = next(iter(ACTIVE_TERMINAL_BINDINGS.values()))
            else:
                return {
                    "status": "ignorado",
                    "detalhe": "Sem vinculo ativo para este terminal",
                    "terminal_id": terminal_id,
                }

        machine_id = binding["machine_id"]
        amount = float(payment_data.get("transaction_amount") or 1.0)

        db = SessionLocal()
        try:
            transacao = Transacao(
                maquina_id=machine_id,
                tipo=EventoTipo.in_flux,
                metodo=MetodoPagamento.digital,
                valor=amount,
                data_hora=datetime.utcnow(),
            )
            db.add(transacao)
            db.add(
                HistoricoOperacao(
                    maquina_id=machine_id,
                    categoria="PAGAMENTO",
                    descricao=f"Pagamento maquininha aprovado (payment_id={payment_id}, terminal_id={terminal_id or 'n/a'})",
                    valor=amount,
                    created_at=transacao.data_hora,
                )
            )
            db.commit()
        finally:
            db.close()

        pulsos = _calcular_pulsos_por_valor(amount)
        publish_machine_credit_pulses(machine_id, pulses=pulsos, action="paid")
        PROCESSED_PAYMENT_IDS.add(payment_id)
        return {
            "status": "sucesso",
            "detalhe": "Pagamento recebido e pulsos enviados",
            "machine_id": machine_id,
            "terminal_id": terminal_id,
            "pulsos": pulsos,
        }

    return {"status": "ignorado", "detalhe": "Evento nao tratado neste endpoint"}


@router.post("/pagamentos/lancar", response_model=PagamentoOut)
def lancar_pagamento(
    pagamento: PagamentoCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    _get_maquina_visivel(db, pagamento.maquina_id, role, cliente_id)

    if pagamento.produto_id is not None:
        produto = db.query(Produto).filter(Produto.id == pagamento.produto_id).first()
        if not produto:
            raise HTTPException(status_code=404, detail="Produto nao encontrado")
        if produto.maquina_id != pagamento.maquina_id:
            raise HTTPException(status_code=400, detail="Produto nao pertence a maquina informada")

    transacao = Transacao(
        maquina_id=pagamento.maquina_id,
        tipo=EventoTipo.in_flux,
        metodo=MetodoPagamento.digital,
        valor=pagamento.valor,
        data_hora=datetime.utcnow(),
    )
    db.add(transacao)
    db.add(
        HistoricoOperacao(
            maquina_id=pagamento.maquina_id,
            categoria="PAGAMENTO",
            descricao=pagamento.descricao or "Pagamento digital lancado pelo painel",
            valor=pagamento.valor,
            created_at=transacao.data_hora,
        )
    )
    db.commit()
    db.refresh(transacao)

    try:
        pulsos = _calcular_pulsos_por_valor(pagamento.valor)
        payload = publish_machine_credit_pulses(
            pagamento.maquina_id,
            pulses=pulsos,
            action="paid",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Falha ao enviar comando MQTT para a maquina") from exc

    return {
        "ok": True,
        "maquina_id": pagamento.maquina_id,
        "valor": pagamento.valor,
        "produto_id": pagamento.produto_id,
        "pulsos": pulsos,
        "payload": payload,
        "data_hora": transacao.data_hora,
    }


@router.post("/pagamentos/terminal/cobrar")
def cobrar_na_maquininha(
    dados: dict,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    machine_id = (dados.get("maquina_id") or "").strip()
    terminal_id = (dados.get("terminal_id") or "").strip()
    valor = float(dados.get("valor") or 0)
    mp_token = (dados.get("mp_access_token") or settings.MP_ACCESS_TOKEN or "").strip()

    if not machine_id:
        raise HTTPException(status_code=422, detail="maquina_id e obrigatorio")
    if not terminal_id:
        raise HTTPException(status_code=422, detail="terminal_id e obrigatorio")
    if valor <= 0:
        raise HTTPException(status_code=422, detail="valor deve ser maior que zero")
    if not mp_token:
        raise HTTPException(status_code=422, detail="MP_ACCESS_TOKEN nao configurado")

    _get_maquina_visivel(db, machine_id, role, cliente_id)

    external_reference = f"{machine_id}:{int(time.time())}"
    body = {
        "type": "point",
        "external_reference": external_reference,
        "description": f"Pagamento maquina {machine_id}",
        "transactions": {
            "payments": [
                {"amount": f"{valor:.2f}"},
            ]
        },
        "config": {
            "point": {
                "terminal_id": terminal_id,
                "print_on_terminal": "no_ticket",
            }
        },
    }
    order_data = _mp_request(
        "POST",
        "https://api.mercadopago.com/v1/orders",
        mp_token,
        body=body,
        headers={"X-Idempotency-Key": f"{machine_id}-{int(time.time() * 1000)}"},
    )

    db.add(
        HistoricoOperacao(
            maquina_id=machine_id,
            categoria="TESTE",
            descricao=f"Cobranca enviada para maquininha (mp_order_id={order_data.get('id')}, terminal_id={terminal_id})",
            valor=valor,
            created_at=datetime.utcnow(),
        )
    )
    db.commit()

    return {
        "ok": True,
        "maquina_id": machine_id,
        "terminal_id": terminal_id,
        "valor": valor,
        "mp_order_id": order_data.get("id"),
        "status": order_data.get("status"),
        "external_reference": external_reference,
    }


@router.post("/pagamentos/escuta/iniciar")
def iniciar_escuta_terminal(
    dados: dict,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    machine_id = (dados.get("maquina_id") or "").strip()
    terminal_id = (dados.get("terminal_id") or "").strip()
    if not machine_id:
        raise HTTPException(status_code=422, detail="maquina_id e obrigatorio")
    if not terminal_id:
        raise HTTPException(status_code=422, detail="terminal_id e obrigatorio")

    _get_maquina_visivel(db, machine_id, role, cliente_id)
    ACTIVE_TERMINAL_BINDINGS[terminal_id] = {
        "machine_id": machine_id,
        "started_at": datetime.utcnow().isoformat(),
    }
    return {
        "ok": True,
        "terminal_id": terminal_id,
        "machine_id": machine_id,
        "mensagem": "Escuta ativada. Aguardando pagamentos aprovados da maquininha.",
    }


@router.post("/pagamentos/escuta/parar")
def parar_escuta_terminal(dados: dict):
    terminal_id = (dados.get("terminal_id") or "").strip()
    if not terminal_id:
        raise HTTPException(status_code=422, detail="terminal_id e obrigatorio")
    existed = ACTIVE_TERMINAL_BINDINGS.pop(terminal_id, None)
    return {"ok": True, "terminal_id": terminal_id, "ativo_antes": bool(existed)}


@router.get("/pagamentos/escuta")
def listar_escutas():
    return {"ativos": ACTIVE_TERMINAL_BINDINGS}

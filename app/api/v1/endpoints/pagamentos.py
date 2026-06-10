from datetime import datetime
from decimal import Decimal
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import Cliente, EscutaTerminal, EventoTipo, HistoricoOperacao, Maquina, MetodoPagamento, Transacao, VendaPagamento
from app.models.produto import Produto
from app.schemas.pagamento import PagamentoCreate, PagamentoOut
from app.services.auditoria import registrar_auditoria
from app.services.mercado_pago import mp_request
from app.services.mqtt_commands import publish_machine_credit_pulses
from app.services.vendas import registrar_venda_pagamento

router = APIRouter()
PROCESSED_PAYMENT_IDS: set[str] = set()


def _calcular_pulsos_por_valor(valor: float) -> int:
    # Regra atual: 1 pulso por R$1, minimo de 1 pulso para qualquer valor positivo.
    quantia = Decimal(str(valor))
    if quantia <= 0:
        return 1
    pulsos = int(quantia)
    return max(1, pulsos)


def _iter_mp_tokens(db: Session):
    seen = set()
    if settings.MP_ACCESS_TOKEN:
        seen.add(settings.MP_ACCESS_TOKEN)
        yield settings.MP_ACCESS_TOKEN
    for token in db.query(Cliente.mp_access_token).filter(Cliente.mp_access_token.isnot(None)).all():
        value = (token[0] or "").strip()
        if value and value not in seen:
            seen.add(value)
            yield value


def _mp_request_with_known_tokens(db: Session, method: str, url: str, preferred_token: str | None = None):
    errors = []
    tokens = []
    if preferred_token:
        tokens.append(preferred_token)
    tokens.extend(list(_iter_mp_tokens(db)))
    for token in tokens:
        try:
            return mp_request(method, url, token), token
        except HTTPException as exc:
            errors.append(str(exc.detail))
    raise HTTPException(
        status_code=502,
        detail="Nao foi possivel consultar o Mercado Pago com as credenciais cadastradas: " + " | ".join(errors[-3:]),
    )


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


def _payment_metadata(payment_data: dict) -> dict:
    issuer = payment_data.get("issuer") or {}
    card = payment_data.get("card") or {}
    return {
        "provider": "mercado_pago",
        "provider_payment_id": str(payment_data.get("id") or "").strip() or None,
        "payment_type": payment_data.get("payment_type_id") or payment_data.get("payment_method_id"),
        "card_brand": payment_data.get("payment_method_id") or card.get("cardholder", {}).get("name"),
        "bank_name": issuer.get("name") or issuer.get("id"),
    }


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

    print(f"[MP webhook] query={payload_query} body={payload_body}")

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
            historico = HistoricoOperacao(
                maquina_id=id_hardware,
                categoria="PAGAMENTO",
                descricao="Pagamento aprovado via callback simplificado",
                valor=valor,
                provider="mercado_pago",
                pulse_status="pendente",
                created_at=nova_transacao.data_hora,
            )
            db.add(historico)
            db.flush()
            registrar_venda_pagamento(
                db,
                maquina_id=id_hardware,
                valor=valor,
                origem="mercado_pago",
                transacao_id=nova_transacao.id,
                historico_id=historico.id,
                provider="mercado_pago",
                status_pulso="pendente",
                created_at=nova_transacao.data_hora,
            )
            db.commit()
        finally:
            db.close()

        pulsos = _calcular_pulsos_por_valor(valor)
        publish_machine_credit_pulses(id_hardware, pulses=pulsos, action="paid")
        print(f"[MP webhook] callback simplificado aprovado maquina={id_hardware} valor={valor} pulsos={pulsos}")
        return {"status": "sucesso", "detalhe": "Pagamento digital registrado", "pulsos": pulsos}

    topic = dados.get("topic") or dados.get("type") or ""
    action = dados.get("action") or ""
    data = dados.get("data") or {}
    order_id = data.get("id") or dados.get("id") or dados.get("data.id")

    if not order_id:
        print("[MP webhook] ignorado: sem id de order/payment")
        return {"status": "ignorado", "detalhe": "Webhook sem id de order/payment"}

    # Para webhook da nova API /v1/orders: type=order action=order.processed
    # Busca detalhes da order para obter external_reference e amount
    if topic == "order" or action.startswith("order.") or str(order_id).startswith("ORD"):
        db_lookup = SessionLocal()
        try:
            order_data, _ = _mp_request_with_known_tokens(
                db_lookup,
                "GET",
                f"https://api.mercadopago.com/v1/orders/{order_id}",
            )
        finally:
            db_lookup.close()
        order_status = (order_data.get("status") or "").lower()
        if order_status not in {"processed"} and action != "order.processed":
            print(f"[MP webhook] order ignorada: status={order_status} action={action}")
            return {"status": "ignorado", "detalhe": f"Order ainda nao aprovada ({order_status or action})"}

        external_reference = order_data.get("external_reference")
        machine_id = _parse_machine_id_from_external_reference(external_reference)
        if not machine_id:
            print("[MP webhook] order ignorada: sem machine_id no external_reference")
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
                print(f"[MP webhook] order duplicada mp_order_id={order_id}")
                return {"status": "ignorado", "detalhe": "Pagamento ja processado"}

            transacao = Transacao(
                maquina_id=machine_id,
                tipo=EventoTipo.in_flux,
                metodo=MetodoPagamento.digital,
                valor=amount,
                data_hora=datetime.utcnow(),
            )
            db.add(transacao)
            historico = HistoricoOperacao(
                maquina_id=machine_id,
                categoria="PAGAMENTO",
                descricao=f"Pagamento aprovado via maquininha MP (mp_order_id={order_id})",
                valor=amount,
                provider="mercado_pago",
                provider_payment_id=str(order_id),
                payment_type="order",
                pulse_status="pendente",
                created_at=transacao.data_hora,
            )
            db.add(historico)
            db.flush()
            registrar_venda_pagamento(
                db,
                maquina_id=machine_id,
                valor=amount,
                origem="mercado_pago",
                transacao_id=transacao.id,
                historico_id=historico.id,
                provider="mercado_pago",
                provider_payment_id=str(order_id),
                tipo_pagamento="order",
                status_pulso="pendente",
                created_at=transacao.data_hora,
            )
            db.commit()
        finally:
            db.close()

        pulsos = _calcular_pulsos_por_valor(amount)
        publish_machine_credit_pulses(machine_id, pulses=pulsos, action="paid")
        db_status = SessionLocal()
        try:
            item = db_status.query(HistoricoOperacao).filter(HistoricoOperacao.id == historico.id).first()
            venda = db_status.query(VendaPagamento).filter(VendaPagamento.historico_id == historico.id).first()
            if item:
                item.pulse_status = "liberado"
            if venda:
                venda.status_pulso = "liberado"
            db_status.commit()
        finally:
            db_status.close()
        print(f"[MP webhook] order processada machine={machine_id} amount={amount} pulsos={pulsos}")
        return {"status": "sucesso", "detalhe": "Pagamento aprovado e pulsos enviados", "pulsos": pulsos}

    # Webhook de pagamento direto na conta MP (ex.: pagamento feito na maquininha vinculada)
    is_payment_event = topic in {"payment"} or action.startswith("payment.")
    if is_payment_event:
        payment_id = str((dados.get("data") or {}).get("id") or dados.get("id") or dados.get("data.id") or "").strip()
        if not payment_id:
            print("[MP webhook] payment ignorado: sem payment_id")
            return {"status": "ignorado", "detalhe": "Evento payment sem id"}
        db_lookup = SessionLocal()
        try:
            payment_data, _ = _mp_request_with_known_tokens(
                db_lookup,
                "GET",
                f"https://api.mercadopago.com/v1/payments/{payment_id}",
            )
        finally:
            db_lookup.close()
        payment_status = (payment_data.get("status") or "").lower()
        if payment_status not in {"approved", "authorized"}:
            print(f"[MP webhook] payment ignorado: payment_id={payment_id} status={payment_status}")
            return {"status": "ignorado", "detalhe": f"Pagamento ainda nao aprovado ({payment_status})"}

        terminal_id = _extract_terminal_id(payment_data)
        amount = float(payment_data.get("transaction_amount") or 1.0)

        db = SessionLocal()
        try:
            duplicado = (
                db.query(HistoricoOperacao)
                .filter(
                    HistoricoOperacao.categoria == "PAGAMENTO",
                    HistoricoOperacao.descricao.contains(f"payment_id={payment_id}"),
                )
                .first()
            )
            if duplicado:
                print(f"[MP webhook] payment duplicado payment_id={payment_id}")
                return {"status": "ignorado", "detalhe": "Pagamento ja processado"}

            escuta = None
            if terminal_id:
                escuta = (
                    db.query(EscutaTerminal)
                    .filter(EscutaTerminal.terminal_id == terminal_id, EscutaTerminal.ativo.is_(True))
                    .first()
                )
            if not escuta:
                escutas_ativas = (
                    db.query(EscutaTerminal)
                    .filter(EscutaTerminal.ativo.is_(True))
                    .order_by(EscutaTerminal.updated_at.desc())
                    .all()
                )
                if escutas_ativas:
                    escuta = escutas_ativas[0]
                    print(
                        f"[MP webhook] terminal sem match exato ({terminal_id}); usando escuta mais recente terminal={escuta.terminal_id} maquina={escuta.maquina_id}"
                    )
                else:
                    print(
                        f"[MP webhook] payment ignorado: sem escuta ativa terminal_id={terminal_id} payment_id={payment_id}"
                    )
                    return {
                        "status": "ignorado",
                        "detalhe": "Sem vinculo ativo para este terminal",
                        "terminal_id": terminal_id,
                    }

            machine_id = escuta.maquina_id
            transacao = Transacao(
                maquina_id=machine_id,
                tipo=EventoTipo.in_flux,
                metodo=MetodoPagamento.digital,
                valor=amount,
                data_hora=datetime.utcnow(),
            )
            db.add(transacao)
            historico = HistoricoOperacao(
                maquina_id=machine_id,
                categoria="PAGAMENTO",
                descricao=f"Pagamento maquininha aprovado (payment_id={payment_id}, terminal_id={terminal_id or 'n/a'})",
                valor=amount,
                created_at=transacao.data_hora,
                **_payment_metadata(payment_data),
            )
            db.add(historico)
            db.flush()
            registrar_venda_pagamento(
                db,
                maquina_id=machine_id,
                valor=amount,
                origem="mercado_pago",
                transacao_id=transacao.id,
                historico_id=historico.id,
                provider="mercado_pago",
                provider_payment_id=payment_id,
                tipo_pagamento=historico.payment_type,
                bandeira_cartao=historico.card_brand,
                banco=historico.bank_name,
                status_pulso="pendente",
                created_at=transacao.data_hora,
            )
            db.commit()
            db.refresh(historico)
        finally:
            db.close()

        pulsos = _calcular_pulsos_por_valor(amount)
        try:
            publish_machine_credit_pulses(machine_id, pulses=pulsos, action="paid")
            db_status = SessionLocal()
            try:
                item = db_status.query(HistoricoOperacao).filter(HistoricoOperacao.id == historico.id).first()
                venda = db_status.query(VendaPagamento).filter(VendaPagamento.historico_id == historico.id).first()
                if item:
                    item.pulse_status = "liberado"
                if venda:
                    venda.status_pulso = "liberado"
                db_status.commit()
            finally:
                db_status.close()
        except Exception:
            db_status = SessionLocal()
            try:
                item = db_status.query(HistoricoOperacao).filter(HistoricoOperacao.id == historico.id).first()
                venda = db_status.query(VendaPagamento).filter(VendaPagamento.historico_id == historico.id).first()
                if item:
                    item.pulse_status = "falha"
                if venda:
                    venda.status_pulso = "falha"
                db_status.commit()
            finally:
                db_status.close()
            raise
        PROCESSED_PAYMENT_IDS.add(payment_id)
        print(
            f"[MP webhook] payment processado payment_id={payment_id} terminal={terminal_id} machine={machine_id} amount={amount} pulsos={pulsos}"
        )
        return {
            "status": "sucesso",
            "detalhe": "Pagamento recebido e pulsos enviados",
            "machine_id": machine_id,
            "terminal_id": terminal_id,
            "pulsos": pulsos,
        }

    print(f"[MP webhook] ignorado: evento nao tratado topic={topic} action={action}")
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
    historico = HistoricoOperacao(
        maquina_id=pagamento.maquina_id,
        categoria="PAGAMENTO",
        descricao=pagamento.descricao or "Pagamento digital lancado pelo painel",
        valor=pagamento.valor,
        provider="manual",
        payment_type="lancamento_painel",
        pulse_status="pendente",
        created_at=transacao.data_hora,
    )
    db.add(historico)
    db.flush()
    registrar_venda_pagamento(
        db,
        maquina_id=pagamento.maquina_id,
        valor=pagamento.valor,
        origem="manual",
        transacao_id=transacao.id,
        historico_id=historico.id,
        provider="manual",
        tipo_pagamento="lancamento_painel",
        status_pulso="pendente",
        conta_faturamento=False,
        conta_ticket_medio=False,
        is_manual=True,
        created_at=transacao.data_hora,
    )
    registrar_auditoria(
        db,
        user,
        acao="PAGAMENTO_MANUAL_LANCADO",
        entidade_tipo="maquina",
        entidade_id=pagamento.maquina_id,
        descricao=(
            f"Pagamento manual lancado valor={pagamento.valor} produto_id={pagamento.produto_id} "
            f"descricao={pagamento.descricao or 'Pagamento digital lancado pelo painel'}"
        ),
    )
    db.commit()
    db.refresh(transacao)
    db.refresh(historico)

    try:
        pulsos = _calcular_pulsos_por_valor(pagamento.valor)
        payload = publish_machine_credit_pulses(
            pagamento.maquina_id,
            pulses=pulsos,
            action="paid",
        )
        historico.pulse_status = "liberado"
        venda = db.query(VendaPagamento).filter(VendaPagamento.historico_id == historico.id).first()
        if venda:
            venda.status_pulso = "liberado"
        db.commit()
    except Exception as exc:
        historico.pulse_status = "falha"
        venda = db.query(VendaPagamento).filter(VendaPagamento.historico_id == historico.id).first()
        if venda:
            venda.status_pulso = "falha"
        db.commit()
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

    if not machine_id:
        raise HTTPException(status_code=422, detail="maquina_id e obrigatorio")
    if not terminal_id:
        raise HTTPException(status_code=422, detail="terminal_id e obrigatorio")
    if valor <= 0:
        raise HTTPException(status_code=422, detail="valor deve ser maior que zero")
    maquina = _get_maquina_visivel(db, machine_id, role, cliente_id)
    mp_token = (
        dados.get("mp_access_token")
        or (maquina.dono.mp_access_token if getattr(maquina, "dono", None) else "")
        or settings.MP_ACCESS_TOKEN
        or ""
    ).strip()
    if not mp_token:
        raise HTTPException(status_code=422, detail="Cliente da maquina sem MP_ACCESS_TOKEN cadastrado")

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
    order_data = mp_request(
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
    escuta = db.query(EscutaTerminal).filter(EscutaTerminal.terminal_id == terminal_id).first()
    now = datetime.utcnow()
    if escuta:
        escuta.maquina_id = machine_id
        escuta.ativo = True
        escuta.updated_at = now
    else:
        escuta = EscutaTerminal(
            terminal_id=terminal_id,
            maquina_id=machine_id,
            ativo=True,
            created_at=now,
            updated_at=now,
        )
        db.add(escuta)
    db.commit()
    return {
        "ok": True,
        "terminal_id": terminal_id,
        "machine_id": machine_id,
        "mensagem": "Escuta ativada. Aguardando pagamentos aprovados da maquininha.",
    }


@router.post("/pagamentos/escuta/parar")
def parar_escuta_terminal(dados: dict, db: Session = Depends(get_db)):
    terminal_id = (dados.get("terminal_id") or "").strip()
    if not terminal_id:
        raise HTTPException(status_code=422, detail="terminal_id e obrigatorio")
    escuta = db.query(EscutaTerminal).filter(EscutaTerminal.terminal_id == terminal_id).first()
    ativo_antes = bool(escuta and escuta.ativo)
    if escuta:
        escuta.ativo = False
        escuta.updated_at = datetime.utcnow()
        db.commit()
    return {"ok": True, "terminal_id": terminal_id, "ativo_antes": ativo_antes}


@router.get("/pagamentos/escuta")
def listar_escutas(db: Session = Depends(get_db)):
    escutas = db.query(EscutaTerminal).filter(EscutaTerminal.ativo.is_(True)).all()
    return {
        "ativos": [
            {
                "terminal_id": item.terminal_id,
                "machine_id": item.maquina_id,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in escutas
        ]
    }

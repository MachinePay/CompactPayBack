from datetime import datetime
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import Cliente, EventoTipo, HistoricoOperacao, Maquina, MetodoPagamento, Transacao, VendaPagamento
from app.models.produto import Produto
from app.schemas.pagamento import PagamentoCreate, PagamentoOut
from app.services.auditoria import registrar_auditoria
from app.services.mercado_pago import mp_request
from app.services.mercado_pago_webhook import processar_callback_mercado_pago
from app.services.mqtt_commands import publish_machine_credit_pulses
from app.services.pagamentos_helpers import calcular_pulsos_por_valor
from app.services.vendas import registrar_venda_pagamento

router = APIRouter()


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
    return processar_callback_mercado_pago(dados)


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
        pulsos = calcular_pulsos_por_valor(pagamento.valor)
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

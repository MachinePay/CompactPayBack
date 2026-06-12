from datetime import datetime
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import AuditoriaOperacao, HistoricoOperacao, Maquina
from app.services.auditoria import registrar_auditoria
from app.services.mercado_pago import mp_request
from app.services.mqtt_commands import publish_machine_credit
from app.services.pulse_tracking import update_pulse_status, wait_for_pulse_confirmation

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_maquina_visivel(db: Session, machine_id: str, role: str, cliente_id):
    query = db.query(Maquina)
    if role != "admin":
        query = query.filter(Maquina.cliente_id == cliente_id)
    maquina = query.filter(Maquina.id_hardware == machine_id).first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")
    return maquina


def _get_user_email(user) -> str:
    token_data, _, _ = user
    return token_data.email


@router.post("/maquinas/{machine_id}/credito-teste")
def enviar_credito_teste(
    machine_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    _get_maquina_visivel(db, machine_id, role, cliente_id)

    command_id = str(uuid4())

    db.add(
        HistoricoOperacao(
            maquina_id=machine_id,
            categoria="TESTE",
            descricao="Credito de teste enviado pelo painel",
            valor=None,
            command_id=command_id,
            pulse_status="pendente",
            created_at=datetime.utcnow(),
        )
    )
    db.add(
        AuditoriaOperacao(
            maquina_id=machine_id,
            acao="TESTE_CREDITO",
            descricao="Credito de teste enviado pelo painel",
            executado_por_email=_get_user_email(user),
            created_at=datetime.utcnow(),
        )
    )
    registrar_auditoria(
        db,
        user,
        acao="TESTE_CREDITO",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=f"Credito de teste enviado pelo painel command_id={command_id}",
    )
    db.commit()

    try:
        update_pulse_status(command_id, "comando_enviado")
        payload = publish_machine_credit(machine_id, action="paid", command_id=command_id)
        pulse_status = wait_for_pulse_confirmation(command_id, timeout_seconds=8)
    except Exception as exc:
        update_pulse_status(command_id, "falha_publicacao")
        raise HTTPException(status_code=502, detail="Falha ao enviar comando MQTT para a maquina") from exc

    if pulse_status != "liberado":
        raise HTTPException(
            status_code=504,
            detail=f"Comando enviado, mas a maquina nao confirmou o pulso ({pulse_status})",
        )

    return {
        "ok": True,
        "machine_id": machine_id,
        "topic": f"/TEF/{machine_id}/cmd",
        "payload": payload,
        "command_id": command_id,
        "pulse_status": pulse_status,
    }


@router.post("/maquinas/{machine_id}/observacoes")
def registrar_observacao_maquina(
    machine_id: str,
    payload: dict,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    _get_maquina_visivel(db, machine_id, role, cliente_id)

    descricao = (payload.get("descricao") or "").strip()
    if not descricao:
        raise HTTPException(status_code=400, detail="Descricao da observacao e obrigatoria")

    historico = HistoricoOperacao(
        maquina_id=machine_id,
        categoria="MANUTENCAO",
        descricao=descricao,
        valor=None,
        created_at=datetime.utcnow(),
    )
    db.add(historico)
    db.add(
        AuditoriaOperacao(
            maquina_id=machine_id,
            acao="OBSERVACAO_REGISTRADA",
            descricao=descricao,
            executado_por_email=_get_user_email(user),
            created_at=datetime.utcnow(),
        )
    )
    registrar_auditoria(
        db,
        user,
        acao="OBSERVACAO_REGISTRADA",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=f"Observacao registrada: {descricao}",
    )
    db.commit()
    db.refresh(historico)
    return {
        "id": historico.id,
        "maquina_id": historico.maquina_id,
        "categoria": historico.categoria,
        "descricao": historico.descricao,
        "valor": historico.valor,
        "created_at": historico.created_at,
    }


@router.post("/maquinas/{machine_id}/pagamentos/{historico_id}/extorno")
def estornar_pagamento_maquina(
    machine_id: str,
    historico_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    maquina = _get_maquina_visivel(db, machine_id, role, cliente_id)
    historico = (
        db.query(HistoricoOperacao)
        .filter(
            HistoricoOperacao.id == historico_id,
            HistoricoOperacao.maquina_id == machine_id,
            HistoricoOperacao.categoria == "PAGAMENTO",
        )
        .first()
    )
    if not historico:
        raise HTTPException(status_code=404, detail="Pagamento nao encontrado")
    if historico.refunded_at:
        raise HTTPException(status_code=400, detail="Pagamento ja foi estornado")
    if (historico.pulse_status or "").lower() != "falha":
        raise HTTPException(status_code=422, detail="Extorno automatico permitido apenas quando o pulso falhou")

    payment_id = historico.provider_payment_id
    if not payment_id:
        match = re.search(r"payment_id=([^,\)\s]+)", historico.descricao or "")
        payment_id = match.group(1) if match else None
    if not payment_id:
        raise HTTPException(status_code=422, detail="Pagamento sem payment_id do Mercado Pago para estorno automatico")

    token = (maquina.dono.mp_access_token if getattr(maquina, "dono", None) else "") or ""
    if not token:
        raise HTTPException(status_code=422, detail="Cliente sem token Mercado Pago para estorno")

    mp_request(
        "POST",
        f"https://api.mercadopago.com/v1/payments/{payment_id}/refunds",
        token.strip(),
        body={},
        headers={"X-Idempotency-Key": f"refund-{payment_id}-{historico_id}"},
    )
    historico.refunded_at = datetime.utcnow()
    db.add(
        AuditoriaOperacao(
            maquina_id=machine_id,
            acao="EXTORNO",
            descricao=f"Extorno solicitado para payment_id={payment_id}",
            executado_por_email=_get_user_email(user),
        )
    )
    registrar_auditoria(
        db,
        user,
        acao="EXTORNO",
        entidade_tipo="pagamento",
        entidade_id=historico_id,
        descricao=f"Extorno Mercado Pago solicitado maquina_id={machine_id} payment_id={payment_id}",
    )
    db.commit()
    return {"ok": True, "payment_id": payment_id, "refunded_at": historico.refunded_at}

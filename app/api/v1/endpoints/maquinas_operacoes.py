from datetime import datetime, timedelta
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import AuditoriaOperacao, FirmwareVersion, HistoricoOperacao, Maquina
from app.services.auditoria import registrar_auditoria
from app.services.mercado_pago import mp_request
from app.services.mqtt_commands import publish_machine_credit, publish_machine_update
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

    if pulse_status != "pulso_confirmado":
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


@router.post("/maquinas/{machine_id}/atualizacao")
def enviar_atualizacao_firmware(
    machine_id: str,
    payload: dict | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode enviar atualizacao de firmware")
    maquina = _get_maquina_visivel(db, machine_id, role, cliente_id)

    if not maquina.ultimo_sinal or datetime.utcnow() - maquina.ultimo_sinal > timedelta(seconds=90):
        raise HTTPException(status_code=409, detail="Maquina offline. Aguarde ela ficar online para atualizar.")

    firmware_record = None
    firmware_version_id = (payload or {}).get("firmware_version_id")
    if firmware_version_id:
        try:
            firmware_version_id = int(firmware_version_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Versao de firmware invalida") from exc
        firmware_record = (
            db.query(FirmwareVersion)
            .filter(FirmwareVersion.id == firmware_version_id, FirmwareVersion.ativo.is_(True))
            .first()
        )
        if not firmware_record:
            raise HTTPException(status_code=404, detail="Versao de firmware nao encontrada ou inativa")

    firmware_url = (
        (firmware_record.url_bin if firmware_record else None)
        or (payload or {}).get("url")
        or settings.OTA_FIRMWARE_URL
        or ""
    ).strip()
    firmware_version = (
        (firmware_record.nome if firmware_record else None)
        or (payload or {}).get("version")
        or ""
    ).strip()
    if not firmware_url:
        raise HTTPException(
            status_code=422,
            detail="Cadastre uma versao de firmware ou configure OTA_FIRMWARE_URL no backend",
        )

    command_id = str(uuid4())
    try:
        mqtt_payload = publish_machine_update(
            machine_id,
            firmware_url,
            firmware_version=firmware_version or None,
            command_id=command_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Falha ao enviar comando MQTT de atualizacao") from exc

    maquina.firmware_target_version = firmware_version or None
    maquina.firmware_update_status = "sent"
    maquina.firmware_update_command_id = command_id
    maquina.firmware_update_url = firmware_url
    maquina.firmware_update_requested_at = datetime.utcnow()
    maquina.firmware_update_started_at = None
    maquina.firmware_update_finished_at = None

    db.add(
        HistoricoOperacao(
            maquina_id=machine_id,
            categoria="DISPOSITIVO",
            descricao=f"Atualizacao OTA enviada url={firmware_url} version={firmware_version or 'n/a'}",
            valor=None,
            command_id=command_id,
            pulse_status="update_enviado",
            created_at=datetime.utcnow(),
        )
    )

    db.add(
        AuditoriaOperacao(
            maquina_id=machine_id,
            acao="ATUALIZACAO_FIRMWARE",
            descricao=f"Atualizacao OTA enviada command_id={command_id} version={firmware_version or 'n/a'} url={firmware_url}",
            executado_por_email=_get_user_email(user),
            created_at=datetime.utcnow(),
        )
    )
    registrar_auditoria(
        db,
        user,
        acao="ATUALIZACAO_FIRMWARE",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=f"Atualizacao OTA enviada command_id={command_id} version={firmware_version or 'n/a'} url={firmware_url}",
    )
    db.commit()

    return {
        "ok": True,
        "machine_id": machine_id,
        "topic": f"/TEF/{machine_id}/cmd",
        "payload": mqtt_payload,
        "command_id": command_id,
        "firmware_version": firmware_version or None,
        "firmware_url": firmware_url,
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
    if not (historico.pulse_status or "").lower().startswith("falha"):
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

from datetime import datetime
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import (
    AuditoriaOperacao,
    EscutaTerminal,
    FechamentoMaquina,
    HistoricoOperacao,
    Maquina,
    Transacao,
    VendaPagamento,
)
from app.models.produto import Produto
from app.schemas.fechamento import FechamentoMaquinaOut
from app.services.auditoria import registrar_auditoria
from app.services.maquinas_relatorio import build_machine_history_payload
from app.services.mercado_pago import mp_request
from app.services.mqtt_commands import publish_machine_credit

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _generate_machine_id(db: Session) -> str:
    numeric_ids = []
    for (id_hardware,) in db.query(Maquina.id_hardware).all():
        value = str(id_hardware or "").strip()
        if value.isdigit():
            numeric_ids.append(int(value))

    next_id = max(numeric_ids, default=999) + 1
    next_id = max(next_id, 1000)
    while db.query(Maquina).filter(Maquina.id_hardware == str(next_id)).first():
        next_id += 1
    return str(next_id)


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


@router.post("/maquinas/{machine_id}/fechamentos", response_model=FechamentoMaquinaOut)
def criar_fechamento_maquina(
    machine_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    periodo: str = "mes",
    data_inicio: str = None,
    data_fim: str = None,
):
    _, role, cliente_id = user
    maquina = _get_maquina_visivel(db, machine_id, role, cliente_id)
    payload = build_machine_history_payload(
        db,
        maquina,
        periodo=periodo,
        data_inicio=data_inicio,
        data_fim=data_fim,
    )
    start_dt = payload["range"]["inicio"]
    end_dt = payload["range"]["fim"]

    fechamento_existente = (
        db.query(FechamentoMaquina)
        .filter(
            FechamentoMaquina.maquina_id == machine_id,
            FechamentoMaquina.periodo_inicio <= end_dt,
            FechamentoMaquina.periodo_fim >= start_dt,
        )
        .first()
    )
    if fechamento_existente:
        raise HTTPException(status_code=409, detail="Ja existe fechamento salvo para esse periodo")

    fechamento = FechamentoMaquina(
        maquina_id=machine_id,
        periodo_inicio=start_dt,
        periodo_fim=end_dt,
        total_pagamentos=payload["resumo"]["total_pagamentos"],
        total_digital=payload["resumo"]["total_digital"],
        total_fisico=payload["resumo"]["total_fisico"],
        quantidade_pagamentos=payload["resumo"]["quantidade_pagamentos"],
        quantidade_testes=payload["resumo"]["quantidade_testes"],
        quantidade_saidas=payload["resumo"]["quantidade_saidas"],
        criado_por_email=_get_user_email(user),
        created_at=datetime.utcnow(),
    )
    db.add(fechamento)
    db.add(
        AuditoriaOperacao(
            maquina_id=machine_id,
            acao="FECHAMENTO_CRIADO",
            descricao=f"Fechamento salvo para o periodo {start_dt.isoformat()} ate {end_dt.isoformat()}",
            executado_por_email=_get_user_email(user),
            created_at=datetime.utcnow(),
        )
    )
    registrar_auditoria(
        db,
        user,
        acao="FECHAMENTO_CRIADO",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=(
            f"Fechamento criado periodo={start_dt.isoformat()} ate {end_dt.isoformat()} "
            f"total={payload['resumo']['total_pagamentos']}"
        ),
    )
    db.commit()
    db.refresh(fechamento)
    return fechamento


@router.get("/maquinas/novo-id")
def gerar_novo_id_maquina(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode gerar ids de maquinas")
    return {"id_hardware": _generate_machine_id(db)}


@router.delete("/maquinas/{machine_id}")
def deletar_maquina(
    machine_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode excluir maquinas")

    db_maquina = db.query(Maquina).filter(Maquina.id_hardware == machine_id).first()
    if not db_maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")

    produtos_removidos = db.query(Produto).filter(Produto.maquina_id == machine_id).count()
    transacoes_removidas = db.query(Transacao).filter(Transacao.maquina_id == machine_id).count()
    vendas_removidas = db.query(VendaPagamento).filter(VendaPagamento.maquina_id == machine_id).count()
    historicos_removidos = db.query(HistoricoOperacao).filter(HistoricoOperacao.maquina_id == machine_id).count()
    fechamentos_removidos = db.query(FechamentoMaquina).filter(FechamentoMaquina.maquina_id == machine_id).count()
    auditorias_removidas = db.query(AuditoriaOperacao).filter(AuditoriaOperacao.maquina_id == machine_id).count()
    escutas_removidas = db.query(EscutaTerminal).filter(EscutaTerminal.maquina_id == machine_id).count()
    registrar_auditoria(
        db,
        user,
        acao="MAQUINA_EXCLUIDA",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=(
            f"Maquina excluida nome={db_maquina.nome_local} cliente_id={db_maquina.cliente_id} "
            f"produtos={produtos_removidos} transacoes={transacoes_removidas} "
            f"vendas={vendas_removidas} escutas_terminal={escutas_removidas} "
            f"historicos={historicos_removidos} fechamentos={fechamentos_removidos} "
            f"auditorias_maquina={auditorias_removidas}"
        ),
    )
    db.query(EscutaTerminal).filter(EscutaTerminal.maquina_id == machine_id).delete(synchronize_session=False)
    db.query(VendaPagamento).filter(VendaPagamento.maquina_id == machine_id).delete(synchronize_session=False)
    db.query(AuditoriaOperacao).filter(AuditoriaOperacao.maquina_id == machine_id).delete(synchronize_session=False)
    db.query(FechamentoMaquina).filter(FechamentoMaquina.maquina_id == machine_id).delete(synchronize_session=False)
    db.query(HistoricoOperacao).filter(HistoricoOperacao.maquina_id == machine_id).delete(synchronize_session=False)
    db.query(Transacao).filter(Transacao.maquina_id == machine_id).delete(synchronize_session=False)
    db.query(Produto).filter(Produto.maquina_id == machine_id).delete(synchronize_session=False)
    db.delete(db_maquina)
    db.commit()
    return {"ok": True}


@router.post("/maquinas/{machine_id}/credito-teste")
def enviar_credito_teste(
    machine_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    _get_maquina_visivel(db, machine_id, role, cliente_id)

    try:
        payload = publish_machine_credit(machine_id, action="paid")
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Falha ao enviar comando MQTT para a maquina") from exc

    db.add(
        HistoricoOperacao(
            maquina_id=machine_id,
            categoria="TESTE",
            descricao="Credito de teste enviado pelo painel",
            valor=None,
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
        descricao=f"Credito de teste enviado pelo painel payload={payload}",
    )
    db.commit()

    return {
        "ok": True,
        "machine_id": machine_id,
        "topic": f"/TEF/{machine_id}/cmd",
        "payload": payload,
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

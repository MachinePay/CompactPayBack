from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import AuditoriaOperacao, FechamentoMaquina, HistoricoOperacao, Maquina, Transacao, VendaPagamento
from app.schemas.fechamento import FechamentoMaquinaOut
from app.services.auditoria import registrar_auditoria
from app.services.maquinas_relatorio import build_machine_history_payload, resolve_date_window

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


@router.get("/maquinas/{machine_id}/historico")
def obter_historico_maquina(
    machine_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    periodo: str = "mes",
    data_inicio: str = None,
    data_fim: str = None,
    registro: str = "todos",
    origem: str = "todos",
    forma: str = "todos",
    pulso: str = "todos",
    busca: str = "",
):
    _, role, cliente_id = user
    maquina = _get_maquina_visivel(db, machine_id, role, cliente_id)
    payload = build_machine_history_payload(
        db,
        maquina,
        periodo=periodo,
        data_inicio=data_inicio,
        data_fim=data_fim,
        registro=registro,
        origem=origem,
        forma=forma,
        pulso=pulso,
        busca=busca,
    )
    return {key: value for key, value in payload.items() if key != "range"}


@router.delete("/maquinas/{machine_id}/historico")
def apagar_historico_maquina(
    machine_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    periodo: str = "mes",
    data_inicio: str = None,
    data_fim: str = None,
):
    _, role, cliente_id = user
    _get_maquina_visivel(db, machine_id, role, cliente_id)
    start_dt, end_dt = resolve_date_window(periodo, data_inicio, data_fim)

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
        raise HTTPException(
            status_code=409,
            detail="Nao e permitido apagar historico de um periodo que ja foi fechado",
        )

    vendas_removidas = (
        db.query(VendaPagamento)
        .filter(
            VendaPagamento.maquina_id == machine_id,
            VendaPagamento.created_at >= start_dt,
            VendaPagamento.created_at <= end_dt,
        )
        .delete(synchronize_session=False)
    )
    transacoes_removidas = (
        db.query(Transacao)
        .filter(
            Transacao.maquina_id == machine_id,
            Transacao.tipo == "IN",
            Transacao.data_hora >= start_dt,
            Transacao.data_hora <= end_dt,
        )
        .delete(synchronize_session=False)
    )
    pagamentos_historico_removidos = (
        db.query(HistoricoOperacao)
        .filter(
            HistoricoOperacao.maquina_id == machine_id,
            HistoricoOperacao.categoria == "PAGAMENTO",
            HistoricoOperacao.created_at >= start_dt,
            HistoricoOperacao.created_at <= end_dt,
        )
        .delete(synchronize_session=False)
    )
    testes_removidos = (
        db.query(HistoricoOperacao)
        .filter(
            HistoricoOperacao.maquina_id == machine_id,
            HistoricoOperacao.categoria == "TESTE",
            HistoricoOperacao.created_at >= start_dt,
            HistoricoOperacao.created_at <= end_dt,
        )
        .delete(synchronize_session=False)
    )
    db.add(
        AuditoriaOperacao(
            maquina_id=machine_id,
            acao="HISTORICO_APAGADO",
            descricao=(
                f"Historico apagado para o periodo {start_dt.isoformat()} ate {end_dt.isoformat()} "
                f"(transacoes={transacoes_removidas}, vendas={vendas_removidas}, "
                f"pagamentos_historico={pagamentos_historico_removidos}, testes={testes_removidos})"
            ),
            executado_por_email=_get_user_email(user),
            created_at=datetime.utcnow(),
        )
    )
    registrar_auditoria(
        db,
        user,
        acao="HISTORICO_APAGADO",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=(
            f"Historico apagado periodo={start_dt.isoformat()} ate {end_dt.isoformat()} "
            f"transacoes={transacoes_removidas} vendas={vendas_removidas} "
            f"pagamentos_historico={pagamentos_historico_removidos} testes={testes_removidos}"
        ),
    )
    db.commit()

    return {
        "ok": True,
        "pagamentos_removidos": transacoes_removidas,
        "vendas_removidas": vendas_removidas,
        "pagamentos_historico_removidos": pagamentos_historico_removidos,
        "testes_removidos": testes_removidos,
    }

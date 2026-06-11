from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import (
    AuditoriaOperacao,
    Cliente,
    EscutaTerminal,
    FechamentoMaquina,
    HistoricoOperacao,
    Maquina,
    Transacao,
    VendaPagamento,
)
from app.models.produto import Produto
from app.schemas.fechamento import FechamentoMaquinaOut
from app.schemas.maquina import MaquinaCreate, MaquinaOut, MaquinaUpdate
from app.services.auditoria import registrar_auditoria
from app.services.maquinas_relatorio import build_machine_history_payload, resolve_date_window, serialize_machine_summary
from app.services.mercado_pago import create_pos_for_machine

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _maquina_query_por_usuario(db: Session, role: str, cliente_id):
    if role == "admin":
        return db.query(Maquina)
    return db.query(Maquina).filter(Maquina.cliente_id == cliente_id)


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
    query = _maquina_query_por_usuario(db, role, cliente_id)
    maquina = query.filter(Maquina.id_hardware == machine_id).first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")
    return maquina


def _get_user_email(user) -> str:
    token_data, _, _ = user
    return token_data.email


@router.get("/maquinas", response_model=List[MaquinaOut])
def listar_maquinas(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    periodo: str = "mes",
    data_inicio: str = None,
    data_fim: str = None,
    cliente_id: int = None,
    id_hardware: str = None,
):
    _, role, user_cliente_id = user
    maquinas_query = _maquina_query_por_usuario(db, role, user_cliente_id)
    if role == "admin" and cliente_id is not None:
        maquinas_query = maquinas_query.filter(Maquina.cliente_id == cliente_id)
    if id_hardware:
        maquinas_query = maquinas_query.filter(Maquina.id_hardware == id_hardware)

    maquinas = maquinas_query.order_by(Maquina.nome_local.asc(), Maquina.id_hardware.asc()).all()
    return [
        serialize_machine_summary(
            db,
            maquina,
            periodo=periodo,
            data_inicio=data_inicio,
            data_fim=data_fim,
        )
        for maquina in maquinas
    ]


@router.post("/maquinas", response_model=MaquinaOut)
def criar_maquina(
    maquina: MaquinaCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode criar maquinas")
    machine_id = maquina.id_hardware or _generate_machine_id(db)
    if db.query(Maquina).filter(Maquina.id_hardware == machine_id).first():
        raise HTTPException(status_code=400, detail="Maquina ja cadastrada")
    cliente = db.query(Cliente).filter(Cliente.id == maquina.cliente_id).first()
    if not cliente:
        raise HTTPException(status_code=422, detail="Escolha um usuario/cliente valido para criar a maquina")
    banco_pagamento = (maquina.banco_pagamento or "mercado_pago").strip().lower()
    bancos_validos = {"mercado_pago", "pagbank", "s6pay"}
    if banco_pagamento not in bancos_validos:
        raise HTTPException(status_code=422, detail="Banco de pagamento invalido")
    banco_habilitado = {
        "mercado_pago": bool(cliente.cliente_mercado_pago or cliente.mp_access_token),
        "pagbank": bool(cliente.cliente_pagbank),
        "s6pay": bool(cliente.cliente_s6pay),
    }[banco_pagamento]
    if not banco_habilitado:
        raise HTTPException(status_code=422, detail="O banco escolhido nao esta habilitado para este cliente")
    if banco_pagamento != "mercado_pago":
        raise HTTPException(status_code=501, detail="Integracao deste banco ainda nao foi implementada")
    if not cliente.mp_access_token:
        raise HTTPException(
            status_code=422,
            detail="O usuario escolhido ainda nao tem MP_ACCESS_TOKEN cadastrado",
        )

    db_maquina = Maquina(
        id_hardware=machine_id,
        cliente_id=maquina.cliente_id,
        banco_pagamento=banco_pagamento,
        nome_local=maquina.nome,
        localizacao=maquina.localizacao,
        ultimo_sinal=None,
    )
    pos_data = create_pos_for_machine(cliente, db_maquina)
    db_maquina.mp_store_id = pos_data["mp_store_id"]
    db_maquina.mp_store_external_id = pos_data["mp_store_external_id"]
    db_maquina.mp_pos_id = pos_data["mp_pos_id"]
    db_maquina.mp_pos_external_id = pos_data["mp_pos_external_id"]
    db_maquina.mp_qr_image = pos_data["mp_qr_image"]
    db.add(db_maquina)
    registrar_auditoria(
        db,
        user,
        acao="MAQUINA_CRIADA",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=(
            f"Maquina criada cliente_id={maquina.cliente_id} nome={maquina.nome} "
            f"localizacao={maquina.localizacao} banco={banco_pagamento} mp_pos_id={db_maquina.mp_pos_id}"
        ),
    )
    db.commit()
    db.refresh(db_maquina)

    return {
        **serialize_machine_summary(db, db_maquina, periodo="mes"),
    }


@router.put("/maquinas/{machine_id}", response_model=MaquinaOut)
def atualizar_maquina(
    machine_id: str,
    maquina: MaquinaUpdate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode editar maquinas")

    db_maquina = db.query(Maquina).filter(Maquina.id_hardware == machine_id).first()
    if not db_maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")

    nome_anterior = db_maquina.nome_local
    localizacao_anterior = db_maquina.localizacao
    cliente_anterior = db_maquina.cliente_id
    banco_anterior = db_maquina.banco_pagamento
    db_maquina.nome_local = maquina.nome
    db_maquina.localizacao = maquina.localizacao
    db_maquina.cliente_id = maquina.cliente_id
    if maquina.banco_pagamento:
        db_maquina.banco_pagamento = maquina.banco_pagamento
    registrar_auditoria(
        db,
        user,
        acao="MAQUINA_ATUALIZADA",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=(
            f"Maquina atualizada nome={nome_anterior}->{maquina.nome} "
            f"localizacao={localizacao_anterior}->{maquina.localizacao} "
            f"cliente_id={cliente_anterior}->{maquina.cliente_id} "
            f"banco={banco_anterior}->{db_maquina.banco_pagamento}"
        ),
    )
    db.commit()
    db.refresh(db_maquina)

    return serialize_machine_summary(db, db_maquina, periodo="mes")


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

from datetime import date, datetime, timedelta
import secrets
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.v1.endpoints import auth, pagamentos, produtos, usuarios
from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import AuditoriaOperacao, FechamentoMaquina, HistoricoOperacao, Maquina, Transacao
from app.schemas.auditoria import AuditoriaOperacaoOut
from app.schemas.fechamento import FechamentoMaquinaOut
from app.schemas.historico import HistoricoOperacaoOut
from app.schemas.maquina import MaquinaCreate, MaquinaOut, MaquinaUpdate
from app.schemas.transacao import TransacaoOut
from app.services.mqtt_commands import publish_machine_credit

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
    while True:
        candidate = f"CPM-{secrets.token_hex(3).upper()}"
        exists = db.query(Maquina).filter(Maquina.id_hardware == candidate).first()
        if not exists:
            return candidate


def _apply_transacao_periodo(
    query,
    periodo: str | None = None,
    data_inicio: str | None = None,
    data_fim: str | None = None,
):
    if data_inicio and data_fim:
        dt_inicio = datetime.fromisoformat(data_inicio)
        dt_fim = datetime.fromisoformat(data_fim)
        return query.filter(
            Transacao.data_hora >= dt_inicio,
            Transacao.data_hora <= dt_fim,
        )

    if periodo == "dia":
        hoje = date.today()
        return query.filter(func.date(Transacao.data_hora) == hoje)

    if periodo == "mes":
        hoje = date.today()
        return query.filter(func.extract("month", Transacao.data_hora) == hoje.month).filter(
            func.extract("year", Transacao.data_hora) == hoje.year
        )

    return query


def _resolve_date_window(
    periodo: str | None = None,
    data_inicio: str | None = None,
    data_fim: str | None = None,
):
    if data_inicio and data_fim:
        return (
            datetime.fromisoformat(data_inicio),
            datetime.fromisoformat(data_fim) + timedelta(days=1) - timedelta(microseconds=1),
        )

    hoje = date.today()
    if periodo == "dia":
        start = datetime.combine(hoje, datetime.min.time())
        end = datetime.combine(hoje, datetime.max.time())
        return start, end

    if periodo == "mes":
        start = datetime.combine(hoje.replace(day=1), datetime.min.time())
        end = datetime.combine(hoje, datetime.max.time())
        return start, end

    end = datetime.combine(hoje, datetime.max.time())
    start = end - timedelta(days=6)
    return start, end


def _get_maquina_visivel(db: Session, machine_id: str, role: str, cliente_id):
    query = _maquina_query_por_usuario(db, role, cliente_id)
    maquina = query.filter(Maquina.id_hardware == machine_id).first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")
    return maquina


def _get_user_email(user) -> str:
    token_data, _, _ = user
    return token_data.email


def _build_machine_history_payload(
    db: Session,
    maquina: Maquina,
    periodo: str = "mes",
    data_inicio: str = None,
    data_fim: str = None,
):
    machine_id = maquina.id_hardware
    transacoes_query = db.query(Transacao).filter(
        Transacao.maquina_id == machine_id,
    )
    transacoes_query = _apply_transacao_periodo(
        transacoes_query,
        periodo=periodo,
        data_inicio=data_inicio,
        data_fim=data_fim,
    )

    pagamentos = transacoes_query.filter(
        Transacao.tipo == "IN",
    ).order_by(Transacao.data_hora.desc()).all()

    saidas = transacoes_query.filter(
        Transacao.tipo == "OUT",
    ).order_by(Transacao.data_hora.desc()).all()

    start_dt, end_dt = _resolve_date_window(periodo, data_inicio, data_fim)
    testes = (
        db.query(HistoricoOperacao)
        .filter(
            HistoricoOperacao.maquina_id == machine_id,
            HistoricoOperacao.categoria == "TESTE",
            HistoricoOperacao.created_at >= start_dt,
            HistoricoOperacao.created_at <= end_dt,
        )
        .order_by(HistoricoOperacao.created_at.desc())
        .all()
    )

    total_pagamentos = sum(float(item.valor or 0) for item in pagamentos)
    total_digital = sum(
        float(item.valor or 0)
        for item in pagamentos
        if (item.metodo.value if hasattr(item.metodo, "value") else str(item.metodo)) == "DIGITAL"
    )
    total_fisico = sum(
        float(item.valor or 0)
        for item in pagamentos
        if (item.metodo.value if hasattr(item.metodo, "value") else str(item.metodo)) == "FISICO"
    )
    ultimo_pagamento = pagamentos[0] if pagamentos else None
    ultimo_teste = testes[0] if testes else None
    ultima_saida = saidas[0] if saidas else None
    totais_por_dia = {}
    for pagamento in pagamentos:
        dia = pagamento.data_hora.strftime("%d/%m/%Y")
        totais_por_dia[dia] = totais_por_dia.get(dia, 0.0) + float(pagamento.valor or 0)

    fechamentos = (
        db.query(FechamentoMaquina)
        .filter(FechamentoMaquina.maquina_id == machine_id)
        .order_by(FechamentoMaquina.created_at.desc())
        .limit(20)
        .all()
    )
    auditoria = (
        db.query(AuditoriaOperacao)
        .filter(AuditoriaOperacao.maquina_id == machine_id)
        .order_by(AuditoriaOperacao.created_at.desc())
        .limit(20)
        .all()
    )

    return {
        "range": {
            "inicio": start_dt,
            "fim": end_dt,
        },
        "maquina": {
            "id_hardware": maquina.id_hardware,
            "nome": maquina.nome_local,
            "localizacao": maquina.localizacao,
            "cliente_nome": maquina.dono.nome_empresa if getattr(maquina, "dono", None) else None,
            "status_online": bool(
                maquina.ultimo_sinal and (datetime.utcnow() - maquina.ultimo_sinal) < timedelta(minutes=3)
            ),
            "ultimo_sinal": maquina.ultimo_sinal,
        },
        "resumo": {
            "total_pagamentos": total_pagamentos,
            "total_digital": total_digital,
            "total_fisico": total_fisico,
            "quantidade_pagamentos": len(pagamentos),
            "quantidade_testes": len(testes),
            "quantidade_saidas": len(saidas),
            "ultimo_pagamento_em": ultimo_pagamento.data_hora if ultimo_pagamento else None,
            "ultimo_teste_em": ultimo_teste.created_at if ultimo_teste else None,
            "ultima_saida_em": ultima_saida.data_hora if ultima_saida else None,
        },
        "totais_por_dia": [
            {"dia": dia, "total": round(total, 2)}
            for dia, total in sorted(
                totais_por_dia.items(),
                key=lambda item: datetime.strptime(item[0], "%d/%m/%Y"),
            )
        ],
        "pagamentos": [
            {
                "id": transacao.id,
                "maquina_id": transacao.maquina_id,
                "maquina_nome": maquina.nome_local,
                "tipo": transacao.tipo.value if hasattr(transacao.tipo, "value") else str(transacao.tipo),
                "metodo": transacao.metodo.value if hasattr(transacao.metodo, "value") else str(transacao.metodo),
                "valor": float(transacao.valor),
                "data_hora": transacao.data_hora,
            }
            for transacao in pagamentos
        ],
        "saidas": [
            {
                "id": transacao.id,
                "maquina_id": transacao.maquina_id,
                "maquina_nome": maquina.nome_local,
                "tipo": transacao.tipo.value if hasattr(transacao.tipo, "value") else str(transacao.tipo),
                "metodo": transacao.metodo.value if hasattr(transacao.metodo, "value") else str(transacao.metodo),
                "valor": float(transacao.valor),
                "data_hora": transacao.data_hora,
            }
            for transacao in saidas
        ],
        "testes": [
            {
                "id": teste.id,
                "maquina_id": teste.maquina_id,
                "categoria": teste.categoria,
                "descricao": teste.descricao,
                "valor": teste.valor,
                "created_at": teste.created_at,
            }
            for teste in testes
        ],
        "fechamentos": [
            {
                "id": fechamento.id,
                "maquina_id": fechamento.maquina_id,
                "periodo_inicio": fechamento.periodo_inicio,
                "periodo_fim": fechamento.periodo_fim,
                "total_pagamentos": float(fechamento.total_pagamentos),
                "total_digital": float(fechamento.total_digital),
                "total_fisico": float(fechamento.total_fisico),
                "quantidade_pagamentos": fechamento.quantidade_pagamentos,
                "quantidade_testes": fechamento.quantidade_testes,
                "quantidade_saidas": fechamento.quantidade_saidas,
                "criado_por_email": fechamento.criado_por_email,
                "created_at": fechamento.created_at,
            }
            for fechamento in fechamentos
        ],
        "auditoria": [
            {
                "id": item.id,
                "maquina_id": item.maquina_id,
                "acao": item.acao,
                "descricao": item.descricao,
                "executado_por_email": item.executado_por_email,
                "created_at": item.created_at,
            }
            for item in auditoria
        ],
    }


@router.get("/maquinas", response_model=List[MaquinaOut])
def listar_maquinas(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    periodo: str = "mes",
    data_inicio: str = None,
    data_fim: str = None,
):
    _, role, cliente_id = user
    maquinas = _maquina_query_por_usuario(db, role, cliente_id).all()
    agora = datetime.utcnow()
    resultado = []

    for maquina in maquinas:
        faturamento_query = (
            db.query(func.sum(Transacao.valor))
            .filter(
                Transacao.maquina_id == maquina.id_hardware,
                Transacao.tipo == "IN",
            )
        )
        faturamento = (
            _apply_transacao_periodo(
                faturamento_query,
                periodo=periodo,
                data_inicio=data_inicio,
                data_fim=data_fim,
            ).scalar()
            or 0.0
        )
        resultado.append(
            {
                "id_hardware": maquina.id_hardware,
                "cliente_id": maquina.cliente_id,
                "nome": maquina.nome_local,
                "localizacao": maquina.localizacao,
                "ultimo_sinal": maquina.ultimo_sinal,
                "status_online": bool(
                    maquina.ultimo_sinal
                    and (agora - maquina.ultimo_sinal) < timedelta(minutes=3)
                ),
                "faturamento": float(faturamento),
            }
        )

    return resultado


@router.get("/maquinas/novo-id")
def gerar_novo_id_maquina(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode gerar ids de maquinas")
    return {"id_hardware": _generate_machine_id(db)}


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

    db_maquina = Maquina(
        id_hardware=machine_id,
        cliente_id=maquina.cliente_id,
        nome_local=maquina.nome,
        localizacao=maquina.localizacao,
        ultimo_sinal=None,
    )
    db.add(db_maquina)
    db.commit()
    db.refresh(db_maquina)

    return {
        "id_hardware": db_maquina.id_hardware,
        "cliente_id": db_maquina.cliente_id,
        "nome": db_maquina.nome_local,
        "localizacao": db_maquina.localizacao,
        "ultimo_sinal": db_maquina.ultimo_sinal,
        "status_online": False,
        "faturamento": 0.0,
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

    db_maquina.nome_local = maquina.nome
    db_maquina.localizacao = maquina.localizacao
    db_maquina.cliente_id = maquina.cliente_id
    db.commit()
    db.refresh(db_maquina)

    faturamento = (
        db.query(func.sum(Transacao.valor))
        .filter(
            Transacao.maquina_id == db_maquina.id_hardware,
            Transacao.tipo == "IN",
        )
        .scalar()
        or 0.0
    )

    return {
        "id_hardware": db_maquina.id_hardware,
        "cliente_id": db_maquina.cliente_id,
        "nome": db_maquina.nome_local,
        "localizacao": db_maquina.localizacao,
        "ultimo_sinal": db_maquina.ultimo_sinal,
        "status_online": bool(
            db_maquina.ultimo_sinal
            and (datetime.utcnow() - db_maquina.ultimo_sinal) < timedelta(minutes=3)
        ),
        "faturamento": float(faturamento),
    }


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

    db.query(AuditoriaOperacao).filter(AuditoriaOperacao.maquina_id == machine_id).delete(synchronize_session=False)
    db.query(FechamentoMaquina).filter(FechamentoMaquina.maquina_id == machine_id).delete(synchronize_session=False)
    db.query(HistoricoOperacao).filter(HistoricoOperacao.maquina_id == machine_id).delete(synchronize_session=False)
    db.query(Transacao).filter(Transacao.maquina_id == machine_id).delete(synchronize_session=False)
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
    maquina = _get_maquina_visivel(db, machine_id, role, cliente_id)

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
    db.commit()

    return {
        "ok": True,
        "machine_id": machine_id,
        "topic": f"/TEF/{machine_id}/cmd",
        "payload": payload,
    }


@router.get("/faturamento")
def faturamento(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    id_hardware: str = None,
    periodo: str = "dia",
    data_inicio: str = None,
    data_fim: str = None,
):
    _, role, cliente_id = user
    query = db.query(Transacao)
    if role != "admin":
        maquinas_ids = [
            m.id_hardware for m in _maquina_query_por_usuario(db, role, cliente_id).all()
        ]
        query = query.filter(Transacao.maquina_id.in_(maquinas_ids))
    if id_hardware:
        query = query.filter(Transacao.maquina_id == id_hardware)
    if data_inicio and data_fim:
        query = _apply_transacao_periodo(query, data_inicio=data_inicio, data_fim=data_fim)
    else:
        query = _apply_transacao_periodo(query, periodo=periodo)

    total = query.with_entities(func.sum(Transacao.valor)).scalar() or 0.0
    return {"faturamento": float(total)}


@router.get("/transacoes", response_model=List[TransacaoOut])
def listar_transacoes(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    id_hardware: str = None,
    tipo: str = None,
    metodo: str = None,
    periodo: str = "mes",
    data_inicio: str = None,
    data_fim: str = None,
    limit: int = 100,
):
    _, role, cliente_id = user
    maquinas = _maquina_query_por_usuario(db, role, cliente_id).all()
    maquinas_por_id = {maquina.id_hardware: maquina for maquina in maquinas}
    maquinas_ids = list(maquinas_por_id.keys())

    query = db.query(Transacao)
    if role != "admin":
        query = query.filter(Transacao.maquina_id.in_(maquinas_ids))
    if id_hardware:
        query = query.filter(Transacao.maquina_id == id_hardware)
    if tipo:
        query = query.filter(Transacao.tipo == tipo.upper())
    if metodo:
        query = query.filter(Transacao.metodo == metodo.upper())

    query = _apply_transacao_periodo(
        query,
        periodo=periodo,
        data_inicio=data_inicio,
        data_fim=data_fim,
    )

    transacoes = (
        query.order_by(Transacao.data_hora.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )

    return [
        {
            "id": transacao.id,
            "maquina_id": transacao.maquina_id,
            "maquina_nome": (
                maquinas_por_id.get(transacao.maquina_id).nome_local
                if maquinas_por_id.get(transacao.maquina_id)
                else None
            ),
            "tipo": transacao.tipo.value if hasattr(transacao.tipo, "value") else str(transacao.tipo),
            "metodo": transacao.metodo.value if hasattr(transacao.metodo, "value") else str(transacao.metodo),
            "valor": float(transacao.valor),
            "data_hora": transacao.data_hora,
        }
        for transacao in transacoes
    ]


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
    payload = _build_machine_history_payload(
        db,
        maquina,
        periodo=periodo,
        data_inicio=data_inicio,
        data_fim=data_fim,
    )
    return {key: value for key, value in payload.items() if key != "range"}


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
    payload = _build_machine_history_payload(
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
    db.commit()
    db.refresh(fechamento)
    return fechamento


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
    start_dt, end_dt = _resolve_date_window(periodo, data_inicio, data_fim)

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

    pagamentos_removidos = (
        db.query(Transacao)
        .filter(
            Transacao.maquina_id == machine_id,
            Transacao.tipo == "IN",
            Transacao.data_hora >= start_dt,
            Transacao.data_hora <= end_dt,
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
                f"(pagamentos={pagamentos_removidos}, testes={testes_removidos})"
            ),
            executado_por_email=_get_user_email(user),
            created_at=datetime.utcnow(),
        )
    )
    db.commit()

    return {
        "ok": True,
        "pagamentos_removidos": pagamentos_removidos,
        "testes_removidos": testes_removidos,
    }


router.include_router(auth.router)
router.include_router(usuarios.router)
router.include_router(produtos.router)
router.include_router(pagamentos.router)


@router.get("/dashboard/stats")
def dashboard_stats(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    hoje = date.today()
    _, role, cliente_id = user
    query = db.query(Transacao)
    if role != "admin":
        maquinas_ids = [
            m.id_hardware for m in _maquina_query_por_usuario(db, role, cliente_id).all()
        ]
        query = query.filter(Transacao.maquina_id.in_(maquinas_ids))

    faturamento = (
        query.with_entities(func.sum(Transacao.valor))
        .filter(
            Transacao.tipo == "IN",
            func.date(Transacao.data_hora) == hoje,
        )
        .scalar()
        or 0.0
    )
    premios = (
        query.with_entities(func.count(Transacao.id))
        .filter(
            Transacao.tipo == "OUT",
            func.date(Transacao.data_hora) == hoje,
        )
        .scalar()
        or 0
    )
    return {
        "faturamento_total_dia": float(faturamento),
        "premios_entregues": int(premios),
    }


@router.get("/dashboard/overview")
def dashboard_overview(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    periodo: str = "mes",
    data_inicio: str = None,
    data_fim: str = None,
):
    _, role, cliente_id = user
    maquinas_query = _maquina_query_por_usuario(db, role, cliente_id)
    maquinas = maquinas_query.all()
    maquinas_ids = [maquina.id_hardware for maquina in maquinas]

    transacoes_query = db.query(Transacao)
    if role != "admin":
        transacoes_query = transacoes_query.filter(Transacao.maquina_id.in_(maquinas_ids))

    transacoes_periodo = _apply_transacao_periodo(
        transacoes_query,
        periodo=periodo,
        data_inicio=data_inicio,
        data_fim=data_fim,
    )

    faturamento = (
        transacoes_periodo.filter(Transacao.tipo == "IN")
        .with_entities(func.sum(Transacao.valor))
        .scalar()
        or 0.0
    )
    premios = (
        transacoes_periodo.filter(Transacao.tipo == "OUT")
        .with_entities(func.count(Transacao.id))
        .scalar()
        or 0
    )

    agora = datetime.utcnow()
    maquinas_online = [
        maquina
        for maquina in maquinas
        if maquina.ultimo_sinal and (agora - maquina.ultimo_sinal) < timedelta(minutes=3)
    ]
    ticket_medio = float(faturamento) / int(premios) if premios else float(faturamento)

    start_dt, end_dt = _resolve_date_window(periodo, data_inicio, data_fim)
    total_days = max(1, (end_dt.date() - start_dt.date()).days + 1)
    chart_data = []
    for index in range(total_days):
        current_day = start_dt.date() + timedelta(days=index)
        day_total = (
            db.query(func.sum(Transacao.valor))
            .filter(
                Transacao.tipo == "IN",
                func.date(Transacao.data_hora) == current_day,
            )
            .filter(Transacao.maquina_id.in_(maquinas_ids) if role != "admin" else True)
            .scalar()
            or 0.0
        )
        chart_data.append(
            {
                "dia": current_day.strftime("%d/%m"),
                "valor": float(day_total),
            }
        )

    zero_movement = []
    for maquina in maquinas:
        movimento = (
            db.query(func.count(Transacao.id))
            .filter(
                Transacao.maquina_id == maquina.id_hardware,
                Transacao.data_hora >= start_dt,
                Transacao.data_hora <= end_dt,
            )
            .scalar()
            or 0
        )
        if movimento == 0:
            zero_movement.append(maquina)

    alerts = []
    for maquina in maquinas:
        if not maquina.ultimo_sinal or (agora - maquina.ultimo_sinal) >= timedelta(minutes=3):
            alerts.append(
                {
                    "title": f"Verificar conectividade da {maquina.nome_local or maquina.id_hardware}",
                    "status": "Offline",
                    "tone": "error",
                }
            )
    for maquina in zero_movement[:4]:
        alerts.append(
            {
                "title": f"Sem movimento em {maquina.nome_local or maquina.id_hardware}",
                "status": "Analise",
                "tone": "warning",
            }
        )

    if not alerts:
        alerts = [
            {
                "title": "Operacao estavel no periodo selecionado",
                "status": "Normal",
                "tone": "success",
            }
        ]

    return {
        "stats": {
            "faturamento_total": float(faturamento),
            "premios_entregues": int(premios),
            "maquinas_ativas": len(maquinas_online),
            "total_maquinas": len(maquinas),
            "ticket_medio": float(ticket_medio),
            "percentual_ativas": round((len(maquinas_online) / len(maquinas)) * 100, 1) if maquinas else 0.0,
            "alertas": len(alerts),
        },
        "chart_data": chart_data,
        "alerts": alerts[:4],
    }

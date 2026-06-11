from datetime import date, datetime, timedelta
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.v1.endpoints import auditoria, auth, clientes, maquinas, mercado_pago, pagamentos, produtos, relatorios, usuarios
from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import Cliente, Maquina, Transacao
from app.schemas.maquina import MaquinaCreate, MaquinaOut, MaquinaUpdate
from app.services.mercado_pago import create_pos_for_machine
from app.services.auditoria import registrar_auditoria
from app.services.maquinas_relatorio import (
    apply_transacao_periodo,
    real_revenue_totals,
    resolve_date_window,
    serialize_machine_summary,
)

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


router.include_router(auth.router)
router.include_router(usuarios.router)
router.include_router(mercado_pago.router)
router.include_router(auditoria.router)
router.include_router(clientes.router)
router.include_router(maquinas.router)
router.include_router(produtos.router)
router.include_router(relatorios.router)
router.include_router(pagamentos.router)


@router.get("/dashboard/stats")
def dashboard_stats(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    hoje = date.today()
    start_dt = datetime.combine(hoje, datetime.min.time())
    end_dt = datetime.combine(hoje, datetime.max.time())
    _, role, cliente_id = user
    query = db.query(Transacao)
    maquinas_ids = [m.id_hardware for m in _maquina_query_por_usuario(db, role, cliente_id).all()]
    if role != "admin":
        query = query.filter(Transacao.maquina_id.in_(maquinas_ids))

    faturamento, _ = real_revenue_totals(db, maquinas_ids, start_dt, end_dt)
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
    cliente_id: int = None,
    id_hardware: str = None,
):
    _, role, user_cliente_id = user
    maquinas_query = _maquina_query_por_usuario(db, role, user_cliente_id)
    if role == "admin" and cliente_id is not None:
        maquinas_query = maquinas_query.filter(Maquina.cliente_id == cliente_id)
    if id_hardware:
        maquinas_query = maquinas_query.filter(Maquina.id_hardware == id_hardware)

    maquinas = maquinas_query.all()
    maquinas_ids = [maquina.id_hardware for maquina in maquinas]

    transacoes_query = db.query(Transacao).filter(Transacao.maquina_id.in_(maquinas_ids))

    transacoes_periodo = apply_transacao_periodo(
        transacoes_query,
        periodo=periodo,
        data_inicio=data_inicio,
        data_fim=data_fim,
    )

    start_dt, end_dt = resolve_date_window(periodo, data_inicio, data_fim)
    faturamento, quantidade_vendas_reais = real_revenue_totals(db, maquinas_ids, start_dt, end_dt)
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
    ticket_medio = float(faturamento) / int(quantidade_vendas_reais) if quantidade_vendas_reais else 0.0

    total_days = max(1, (end_dt.date() - start_dt.date()).days + 1)
    chart_data = []
    for index in range(total_days):
        current_day = start_dt.date() + timedelta(days=index)
        day_total = 0.0
        if maquinas_ids:
            day_total, _ = real_revenue_totals(
                db,
                maquinas_ids,
                datetime.combine(current_day, datetime.min.time()),
                datetime.combine(current_day, datetime.max.time()),
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

    clientes_resumo = []
    clientes_map = {}
    for maquina in maquinas:
        key = maquina.cliente_id or 0
        if key not in clientes_map:
            clientes_map[key] = {
                "cliente_id": maquina.cliente_id,
                "cliente_nome": maquina.dono.nome_empresa if getattr(maquina, "dono", None) else "Sem cliente",
                "maquinas": [],
                "maquinas_online": 0,
            }
        clientes_map[key]["maquinas"].append(maquina)
        if maquina.ultimo_sinal and (agora - maquina.ultimo_sinal) < timedelta(minutes=3):
            clientes_map[key]["maquinas_online"] += 1

    for item in clientes_map.values():
        machine_ids = [maquina.id_hardware for maquina in item["maquinas"]]
        cliente_total = 0.0
        ultima_atividade_em = None
        if machine_ids:
            cliente_total, _ = real_revenue_totals(db, machine_ids, start_dt, end_dt)
            ultima_atividade_em = (
                db.query(func.max(Transacao.data_hora))
                .filter(Transacao.maquina_id.in_(machine_ids))
                .scalar()
            )
        clientes_resumo.append(
            {
                "cliente_id": item["cliente_id"],
                "cliente_nome": item["cliente_nome"],
                "total_faturado": float(cliente_total),
                "maquinas": len(item["maquinas"]),
                "maquinas_online": item["maquinas_online"],
                "ultima_atividade_em": ultima_atividade_em,
            }
        )

    clientes_resumo.sort(key=lambda item: item["total_faturado"], reverse=True)

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
        "clientes_resumo": clientes_resumo[:8],
    }

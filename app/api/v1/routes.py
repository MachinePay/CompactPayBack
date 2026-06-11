from datetime import date, datetime, timedelta
import re
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.api.v1.endpoints import auditoria, auth, clientes, maquinas, mercado_pago, pagamentos, produtos, relatorios, usuarios
from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import AuditoriaOperacao, Cliente, FechamentoMaquina, HistoricoOperacao, Maquina, Transacao, VendaPagamento
from app.schemas.auditoria import AuditoriaOperacaoOut
from app.schemas.historico import HistoricoOperacaoOut
from app.schemas.maquina import MaquinaCreate, MaquinaOut, MaquinaUpdate
from app.services.mercado_pago import create_pos_for_machine
from app.services.auditoria import registrar_auditoria

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


def _real_payment_history_query(db: Session, machine_ids: list[str], start_dt: datetime, end_dt: datetime):
    query = db.query(HistoricoOperacao).filter(
        HistoricoOperacao.categoria == "PAGAMENTO",
        HistoricoOperacao.created_at >= start_dt,
        HistoricoOperacao.created_at <= end_dt,
    )
    if not machine_ids:
        return query.filter(HistoricoOperacao.id.is_(None))
    return query.filter(
        HistoricoOperacao.maquina_id.in_(machine_ids),
        or_(HistoricoOperacao.provider.is_(None), HistoricoOperacao.provider != "manual"),
        ~HistoricoOperacao.descricao.ilike("%lancado pelo painel%"),
    )


def _real_revenue_totals(db: Session, machine_ids: list[str], start_dt: datetime, end_dt: datetime) -> tuple[float, int]:
    if not machine_ids:
        return 0.0, 0
    vendas_query = db.query(VendaPagamento).filter(
        VendaPagamento.maquina_id.in_(machine_ids),
        VendaPagamento.created_at >= start_dt,
        VendaPagamento.created_at <= end_dt,
        VendaPagamento.conta_faturamento.is_(True),
    )
    vendas_total = vendas_query.with_entities(func.sum(VendaPagamento.valor_liquido)).scalar() or 0.0
    vendas_count = (
        vendas_query.filter(VendaPagamento.conta_ticket_medio.is_(True))
        .with_entities(func.count(VendaPagamento.id))
        .scalar()
        or 0
    )

    historicos_com_venda = db.query(VendaPagamento.historico_id).filter(VendaPagamento.historico_id.isnot(None))
    digital_query = _real_payment_history_query(db, machine_ids, start_dt, end_dt).filter(
        ~HistoricoOperacao.id.in_(historicos_com_venda)
    )
    digital_total = digital_query.with_entities(func.sum(HistoricoOperacao.valor)).scalar() or 0.0
    digital_count = digital_query.with_entities(func.count(HistoricoOperacao.id)).scalar() or 0

    transacoes_com_venda = db.query(VendaPagamento.transacao_id).filter(VendaPagamento.transacao_id.isnot(None))
    fisico_query = db.query(Transacao).filter(
        Transacao.maquina_id.in_(machine_ids),
        Transacao.tipo == "IN",
        Transacao.metodo == "FISICO",
        Transacao.data_hora >= start_dt,
        Transacao.data_hora <= end_dt,
        ~Transacao.id.in_(transacoes_com_venda),
    )
    fisico_total = fisico_query.with_entities(func.sum(Transacao.valor)).scalar() or 0.0
    fisico_count = fisico_query.with_entities(func.count(Transacao.id)).scalar() or 0
    return (
        float(vendas_total or 0.0) + float(digital_total or 0.0) + float(fisico_total or 0.0),
        int(vendas_count or 0) + int(digital_count or 0) + int(fisico_count or 0),
    )


def _status_operacional(status_online: bool, ultima_atividade_em: datetime | None) -> str:
    if not status_online:
        return "offline"
    if ultima_atividade_em is None:
        return "atencao"
    return "operando"


def _serialize_machine_summary(
    db: Session,
    maquina: Maquina,
    periodo: str = "mes",
    data_inicio: str = None,
    data_fim: str = None,
):
    agora = datetime.utcnow()
    status_online = bool(
        maquina.ultimo_sinal and (agora - maquina.ultimo_sinal) < timedelta(minutes=3)
    )
    start_dt, end_dt = _resolve_date_window(periodo, data_inicio, data_fim)
    faturamento, _ = _real_revenue_totals(db, [maquina.id_hardware], start_dt, end_dt)
    ultimo_pagamento_em = (
        db.query(func.max(Transacao.data_hora))
        .filter(
            Transacao.maquina_id == maquina.id_hardware,
            Transacao.tipo == "IN",
            Transacao.data_hora >= start_dt,
            Transacao.data_hora <= end_dt,
        )
        .scalar()
    )
    ultima_saida_em = (
        db.query(func.max(Transacao.data_hora))
        .filter(
            Transacao.maquina_id == maquina.id_hardware,
            Transacao.tipo == "OUT",
            Transacao.data_hora >= start_dt,
            Transacao.data_hora <= end_dt,
        )
        .scalar()
    )
    ultimo_teste_em = (
        db.query(func.max(HistoricoOperacao.created_at))
        .filter(
            HistoricoOperacao.maquina_id == maquina.id_hardware,
            HistoricoOperacao.categoria == "TESTE",
            HistoricoOperacao.created_at >= start_dt,
            HistoricoOperacao.created_at <= end_dt,
        )
        .scalar()
    )
    ultima_atividade_em = max(
        [item for item in [ultimo_pagamento_em, ultima_saida_em, ultimo_teste_em] if item is not None],
        default=None,
    )

    return {
        "id_hardware": maquina.id_hardware,
        "cliente_id": maquina.cliente_id,
        "cliente_nome": maquina.dono.nome_empresa if getattr(maquina, "dono", None) else None,
        "nome": maquina.nome_local,
        "localizacao": maquina.localizacao,
        "banco_pagamento": maquina.banco_pagamento or "mercado_pago",
        "mp_store_id": maquina.mp_store_id,
        "mp_store_external_id": maquina.mp_store_external_id,
        "mp_pos_id": maquina.mp_pos_id,
        "mp_pos_external_id": maquina.mp_pos_external_id,
        "mp_qr_image": maquina.mp_qr_image,
        "ultimo_sinal": maquina.ultimo_sinal,
        "ultimo_pagamento_em": ultimo_pagamento_em,
        "ultimo_teste_em": ultimo_teste_em,
        "ultima_saida_em": ultima_saida_em,
        "ultima_atividade_em": ultima_atividade_em,
        "status_online": status_online,
        "status_operacional": _status_operacional(status_online, ultima_atividade_em),
        "faturamento": float(faturamento),
    }


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
    pagamentos_historico = (
        db.query(HistoricoOperacao)
        .filter(
            HistoricoOperacao.maquina_id == machine_id,
            HistoricoOperacao.categoria == "PAGAMENTO",
            HistoricoOperacao.created_at >= start_dt,
            HistoricoOperacao.created_at <= end_dt,
        )
        .order_by(HistoricoOperacao.created_at.desc())
        .all()
    )

    total_pagamentos, quantidade_pagamentos_reais = _real_revenue_totals(db, [machine_id], start_dt, end_dt)
    total_digital = (
        _real_payment_history_query(db, [machine_id], start_dt, end_dt)
        .with_entities(func.sum(HistoricoOperacao.valor))
        .scalar()
        or 0.0
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
    observacoes = (
        db.query(HistoricoOperacao)
        .filter(
            HistoricoOperacao.maquina_id == machine_id,
            HistoricoOperacao.categoria == "MANUTENCAO",
        )
        .order_by(HistoricoOperacao.created_at.desc())
        .limit(20)
        .all()
    )

    timeline = []
    for transacao in pagamentos:
        timeline.append(
            {
                "id": f"pagamento-{transacao.id}",
                "tipo": "pagamento",
                "titulo": "Pagamento registrado",
                "descricao": f"{transacao.metodo.value if hasattr(transacao.metodo, 'value') else str(transacao.metodo)} - R$ {float(transacao.valor):.2f}",
                "created_at": transacao.data_hora,
            }
        )
    for transacao in saidas:
        timeline.append(
            {
                "id": f"saida-{transacao.id}",
                "tipo": "saida",
                "titulo": "Saida registrada",
                "descricao": f"{transacao.metodo.value if hasattr(transacao.metodo, 'value') else str(transacao.metodo)} - R$ {float(transacao.valor):.2f}",
                "created_at": transacao.data_hora,
            }
        )
    for teste in testes:
        timeline.append(
            {
                "id": f"teste-{teste.id}",
                "tipo": "teste",
                "titulo": "Teste enviado",
                "descricao": teste.descricao,
                "created_at": teste.created_at,
            }
        )
    for observacao in observacoes:
        timeline.append(
            {
                "id": f"observacao-{observacao.id}",
                "tipo": "observacao",
                "titulo": "Observacao de manutencao",
                "descricao": observacao.descricao,
                "created_at": observacao.created_at,
            }
        )
    for fechamento in fechamentos:
        timeline.append(
            {
                "id": f"fechamento-{fechamento.id}",
                "tipo": "fechamento",
                "titulo": "Fechamento salvo",
                "descricao": f"Total R$ {float(fechamento.total_pagamentos):.2f}",
                "created_at": fechamento.created_at,
            }
        )
    timeline.sort(key=lambda item: item["created_at"], reverse=True)
    vendas = []
    for item in pagamentos_historico:
        provider_payment_id = item.provider_payment_id
        if not provider_payment_id:
            match = re.search(r"(?:payment_id|mp_order_id)=([^,\)\s]+)", item.descricao or "")
            provider_payment_id = match.group(1) if match else None
        pulse_status = item.pulse_status or "liberado"
        vendas.append(
            {
                "id": item.id,
                "kind": "pagamento",
                "is_test": False,
                "data": item.created_at,
                "valor": float(item.valor or 0),
                "taxa": None,
                "total": float(item.valor or 0),
                "ponto": maquina.nome_local,
                "provider": item.provider or (maquina.banco_pagamento or "mercado_pago"),
                "payment_type": item.payment_type or "digital",
                "card_brand": item.card_brand,
                "bank_name": item.bank_name,
                "provider_payment_id": provider_payment_id,
                "pulse_status": pulse_status,
                "situacao": "Extornado" if item.refunded_at else "Venda Aprovada",
                "refunded_at": item.refunded_at,
                "can_refund": bool(provider_payment_id and item.provider in {None, "mercado_pago"} and not item.refunded_at and pulse_status == "falha"),
                "descricao": item.descricao,
            }
        )
    for item in testes:
        vendas.append(
            {
                "id": item.id,
                "kind": "teste",
                "is_test": True,
                "data": item.created_at,
                "valor": float(item.valor or 0),
                "taxa": None,
                "total": float(item.valor or 0),
                "ponto": maquina.nome_local,
                "provider": "teste",
                "payment_type": "TESTE",
                "card_brand": None,
                "bank_name": None,
                "provider_payment_id": None,
                "pulse_status": "teste",
                "situacao": "TESTE",
                "refunded_at": None,
                "can_refund": False,
                "descricao": item.descricao,
            }
        )
    vendas.sort(key=lambda item: item["data"], reverse=True)

    return {
        "range": {
            "inicio": start_dt,
            "fim": end_dt,
        },
        "maquina": {
            "id_hardware": maquina.id_hardware,
            "nome": maquina.nome_local,
            "localizacao": maquina.localizacao,
            "banco_pagamento": maquina.banco_pagamento or "mercado_pago",
            "mp_pos_id": maquina.mp_pos_id,
            "mp_pos_external_id": maquina.mp_pos_external_id,
            "cliente_nome": maquina.dono.nome_empresa if getattr(maquina, "dono", None) else None,
            "status_online": bool(
                maquina.ultimo_sinal and (datetime.utcnow() - maquina.ultimo_sinal) < timedelta(minutes=3)
            ),
            "ultimo_sinal": maquina.ultimo_sinal,
            "status_operacional": _status_operacional(
                bool(maquina.ultimo_sinal and (datetime.utcnow() - maquina.ultimo_sinal) < timedelta(minutes=3)),
                max(
                    [item for item in [ultimo_pagamento.data_hora if ultimo_pagamento else None, ultimo_teste.created_at if ultimo_teste else None, ultima_saida.data_hora if ultima_saida else None] if item is not None],
                    default=None,
                ),
            ),
        },
        "resumo": {
            "total_pagamentos": total_pagamentos,
            "total_digital": total_digital,
            "total_fisico": total_fisico,
            "quantidade_pagamentos": quantidade_pagamentos_reais,
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
        "vendas": vendas,
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
        "observacoes": [
            {
                "id": item.id,
                "maquina_id": item.maquina_id,
                "categoria": item.categoria,
                "descricao": item.descricao,
                "valor": item.valor,
                "created_at": item.created_at,
            }
            for item in observacoes
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
        "timeline": timeline[:50],
    }


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
        _serialize_machine_summary(
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
        **_serialize_machine_summary(db, db_maquina, periodo="mes"),
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

    return _serialize_machine_summary(db, db_maquina, periodo="mes")


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

    faturamento, _ = _real_revenue_totals(db, maquinas_ids, start_dt, end_dt)
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

    transacoes_periodo = _apply_transacao_periodo(
        transacoes_query,
        periodo=periodo,
        data_inicio=data_inicio,
        data_fim=data_fim,
    )

    start_dt, end_dt = _resolve_date_window(periodo, data_inicio, data_fim)
    faturamento, quantidade_vendas_reais = _real_revenue_totals(db, maquinas_ids, start_dt, end_dt)
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
            day_total, _ = _real_revenue_totals(
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
            cliente_total, _ = _real_revenue_totals(db, machine_ids, start_dt, end_dt)
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

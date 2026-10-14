"""Tela "Ao vivo" (feed em tempo real para TV) e Ranking das maquinas.

Os numeros vem dos mesmos calculos do resto do painel
(compute_financial_summary_by_machine, transacao_summary_by_machine,
quedas_counts_by_machine), para nao divergir do Dashboard/Relatorios.
Cliente ve so as proprias maquinas; admin pode filtrar por cliente.
"""
from datetime import datetime, timedelta
from statistics import median

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, joinedload

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import HistoricoOperacao, Maquina, Transacao, VendaPagamento
from app.services.maquinas_relatorio import (
    BRASILIA_TZ,
    ONLINE_SIGNAL_WINDOW,
    compute_financial_summary_by_machine,
    quedas_counts_by_machine,
    resolve_date_window,
    transacao_summary_by_machine,
    transacao_tipo_out_filter,
)

router = APIRouter()

FEED_JANELA = timedelta(hours=24)
FEED_LIMITE_MAX = 100
# Payout: so compara maquinas com movimento suficiente no periodo.
PAYOUT_MIN_PELUCIAS = 3
PAYOUT_MIN_FATURAMENTO = 30.0
PAYOUT_FATOR_ALERTA = 2.0  # metade ou o dobro da mediana da frota


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _maquinas_do_usuario(db: Session, user, cliente_id: int | None) -> list[Maquina]:
    _, role, cliente_do_usuario = user
    query = db.query(Maquina).options(joinedload(Maquina.dono))
    if role != "admin":
        query = query.filter(Maquina.cliente_id == cliente_do_usuario)
    elif cliente_id is not None:
        query = query.filter(Maquina.cliente_id == cliente_id)
    return query.all()


def _jogadas(valor: float, maquina: Maquina | None) -> int | None:
    """Creditos liberados por um pagamento: valor / valor de cada credito."""
    try:
        valor_credito = float(str(maquina.pulse_value or "").replace(",", ".")) if maquina else 0.0
    except ValueError:
        valor_credito = 0.0
    if valor_credito <= 0:
        return None
    return int((valor or 0) // valor_credito)


def _nome(maquina: Maquina | None, machine_id: str) -> str:
    return (maquina.nome_local if maquina else None) or machine_id


@router.get("/ao-vivo")
def feed_ao_vivo(
    cliente_id: int | None = Query(default=None),
    limit: int = Query(default=40, ge=1, le=FEED_LIMITE_MAX),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Ultimos acontecimentos (pagamentos, pelucias entregues, quedas) das
    ultimas 24h e o resumo de hoje. O painel consulta a cada poucos segundos."""
    maquinas = _maquinas_do_usuario(db, user, cliente_id)
    por_id = {m.id_hardware: m for m in maquinas}
    ids = list(por_id)
    agora = datetime.utcnow()
    desde = agora - FEED_JANELA
    eventos = []

    if ids:
        pagamentos = (
            db.query(VendaPagamento)
            .filter(
                VendaPagamento.maquina_id.in_(ids),
                VendaPagamento.created_at >= desde,
                VendaPagamento.is_teste.is_(False),
            )
            .order_by(VendaPagamento.created_at.desc())
            .limit(limit)
            .all()
        )
        for venda in pagamentos:
            maquina = por_id.get(venda.maquina_id)
            eventos.append(
                {
                    "id": f"p{venda.id}",
                    "tipo": "pagamento",
                    "quando": venda.created_at,
                    "maquina_id": venda.maquina_id,
                    "maquina_nome": _nome(maquina, venda.maquina_id),
                    "valor": float(venda.valor_bruto or 0),
                    "jogadas": _jogadas(venda.valor_bruto, maquina),
                    "forma": "fisico" if venda.origem == "fisico" else (venda.tipo_pagamento or "digital"),
                    "estornado": venda.refunded_at is not None,
                }
            )

        saidas = (
            db.query(Transacao)
            .filter(Transacao.maquina_id.in_(ids), transacao_tipo_out_filter(), Transacao.data_hora >= desde)
            .order_by(Transacao.data_hora.desc())
            .limit(limit)
            .all()
        )
        for saida in saidas:
            eventos.append(
                {
                    "id": f"s{saida.id}",
                    "tipo": "pelucia",
                    "quando": saida.data_hora,
                    "maquina_id": saida.maquina_id,
                    "maquina_nome": _nome(por_id.get(saida.maquina_id), saida.maquina_id),
                }
            )

        quedas = (
            db.query(HistoricoOperacao)
            .filter(
                HistoricoOperacao.maquina_id.in_(ids),
                HistoricoOperacao.categoria == "DISPOSITIVO",
                HistoricoOperacao.created_at >= desde,
                HistoricoOperacao.descricao.ilike("Maquina caiu%"),
            )
            .order_by(HistoricoOperacao.created_at.desc())
            .limit(limit)
            .all()
        )
        for queda in quedas:
            eventos.append(
                {
                    "id": f"q{queda.id}",
                    "tipo": "queda",
                    "quando": queda.created_at,
                    "maquina_id": queda.maquina_id,
                    "maquina_nome": _nome(por_id.get(queda.maquina_id), queda.maquina_id),
                }
            )

    eventos.sort(key=lambda item: item["quando"], reverse=True)
    eventos = eventos[:limit]

    inicio_dia, fim_dia = resolve_date_window("dia")
    financeiro = compute_financial_summary_by_machine(db, ids, inicio_dia, fim_dia) if ids else {}
    saidas_dia = transacao_summary_by_machine(db, ids, inicio_dia, fim_dia) if ids else {}
    faturamento_por_maquina = {mid: float(fin.get("faturamento_total") or 0) for mid, fin in financeiro.items()}
    destaque_id = max(faturamento_por_maquina, key=faturamento_por_maquina.get, default=None)
    online = sum(1 for m in maquinas if m.ultimo_sinal and agora - m.ultimo_sinal < ONLINE_SIGNAL_WINDOW)

    return {
        "agora": agora,
        "eventos": eventos,
        "hoje": {
            "faturamento": round(sum(faturamento_por_maquina.values()), 2),
            "vendas": sum(int(fin.get("vendas_count") or 0) for fin in financeiro.values()),
            "pelucias": sum(int(item.get("quantidade_saidas") or 0) for item in saidas_dia.values()),
            "maquinas_online": online,
            "maquinas_total": len(maquinas),
            "destaque": (
                {
                    "maquina_id": destaque_id,
                    "maquina_nome": _nome(por_id.get(destaque_id), destaque_id),
                    "faturamento": round(faturamento_por_maquina[destaque_id], 2),
                }
                if destaque_id and faturamento_por_maquina[destaque_id] > 0
                else None
            ),
        },
    }


@router.get("/ranking-maquinas")
def ranking_maquinas(
    periodo: str = Query(default="semana", pattern="^(dia|semana|mes)$"),
    cliente_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Por maquina: faturamento, vendas, pelucias, quedas e payout (R$ por
    pelucia), com aviso quando o payout foge muito da mediana da frota."""
    maquinas = _maquinas_do_usuario(db, user, cliente_id)
    ids = [m.id_hardware for m in maquinas]
    inicio, fim = resolve_date_window("dia" if periodo == "dia" else "mes" if periodo == "mes" else None)
    financeiro = compute_financial_summary_by_machine(db, ids, inicio, fim) if ids else {}
    saidas = transacao_summary_by_machine(db, ids, inicio, fim) if ids else {}
    quedas = quedas_counts_by_machine(db, ids, inicio) if ids else {}

    linhas = []
    for maquina in maquinas:
        mid = maquina.id_hardware
        faturamento = float((financeiro.get(mid) or {}).get("faturamento_total") or 0)
        pelucias = int((saidas.get(mid) or {}).get("quantidade_saidas") or 0)
        linhas.append(
            {
                "maquina_id": mid,
                "maquina_nome": _nome(maquina, mid),
                "cliente_nome": maquina.dono.nome_empresa if maquina.dono else None,
                "faturamento": round(faturamento, 2),
                "vendas": int((financeiro.get(mid) or {}).get("vendas_count") or 0),
                "pelucias": pelucias,
                "quedas": int(quedas.get(mid) or 0),
                "reais_por_pelucia": round(faturamento / pelucias, 2) if pelucias else None,
            }
        )

    comparaveis = [
        item["reais_por_pelucia"]
        for item in linhas
        if item["pelucias"] >= PAYOUT_MIN_PELUCIAS and item["faturamento"] >= PAYOUT_MIN_FATURAMENTO
    ]
    mediana = median(comparaveis) if len(comparaveis) >= 2 else None
    for item in linhas:
        item["payout_situacao"] = _situacao_payout(item, mediana)

    return {"periodo": periodo, "mediana_reais_por_pelucia": mediana, "maquinas": linhas}


def _situacao_payout(item: dict, mediana: float | None) -> str:
    """normal | dando_demais (pelucia barata) | segurando (pelucia cara ou
    nenhuma com bastante faturamento) | poucos_dados."""
    if item["faturamento"] < PAYOUT_MIN_FATURAMENTO or mediana is None:
        return "poucos_dados"
    if item["pelucias"] == 0:
        return "segurando" if item["faturamento"] >= mediana * PAYOUT_FATOR_ALERTA else "poucos_dados"
    if item["pelucias"] < PAYOUT_MIN_PELUCIAS:
        return "poucos_dados"
    if item["reais_por_pelucia"] < mediana / PAYOUT_FATOR_ALERTA:
        return "dando_demais"
    if item["reais_por_pelucia"] > mediana * PAYOUT_FATOR_ALERTA:
        return "segurando"
    return "normal"


HORARIOS_PERIODOS_DIAS = {"semana": 7, "mes": 30, "trimestre": 90}


@router.get("/horarios-pico")
def horarios_pico(
    periodo: str = Query(default="mes", pattern="^(semana|mes|trimestre)$"),
    cliente_id: int | None = Query(default=None),
    machine_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Faturamento, vendas e pelucias por dia da semana x hora (horario de
    Brasilia) nos ultimos 7/30/90 dias. "dias" diz quantas vezes cada dia da
    semana aparece no periodo, para o painel mostrar media por dia."""
    from datetime import timezone

    fuso = BRASILIA_TZ
    utc = timezone.utc
    maquinas = _maquinas_do_usuario(db, user, cliente_id)
    ids = [m.id_hardware for m in maquinas if not machine_id or m.id_hardware == machine_id]
    dias_periodo = HORARIOS_PERIODOS_DIAS[periodo]
    agora_local = datetime.now(fuso)
    inicio_local = (agora_local - timedelta(days=dias_periodo - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    inicio_utc = inicio_local.astimezone(utc).replace(tzinfo=None)

    # Python: segunda=0 ... domingo=6
    grade = [[{"faturamento": 0.0, "vendas": 0, "pelucias": 0} for _ in range(24)] for _ in range(7)]

    def local(momento: datetime) -> datetime:
        return momento.replace(tzinfo=utc).astimezone(fuso)

    if ids:
        vendas = (
            db.query(VendaPagamento.created_at, VendaPagamento.valor_bruto)
            .filter(
                VendaPagamento.maquina_id.in_(ids),
                VendaPagamento.created_at >= inicio_utc,
                VendaPagamento.is_teste.is_(False),
                VendaPagamento.refunded_at.is_(None),
                VendaPagamento.conta_faturamento.is_(True),
            )
            .all()
        )
        for criado, valor in vendas:
            momento = local(criado)
            celula = grade[momento.weekday()][momento.hour]
            celula["faturamento"] += float(valor or 0)
            celula["vendas"] += 1
        saidas = (
            db.query(Transacao.data_hora)
            .filter(Transacao.maquina_id.in_(ids), transacao_tipo_out_filter(), Transacao.data_hora >= inicio_utc)
            .all()
        )
        for (momento_utc,) in saidas:
            momento = local(momento_utc)
            grade[momento.weekday()][momento.hour]["pelucias"] += 1

    dias = [0] * 7
    for deslocamento in range(dias_periodo):
        dias[(inicio_local + timedelta(days=deslocamento)).weekday()] += 1

    for linha in grade:
        for celula in linha:
            celula["faturamento"] = round(celula["faturamento"], 2)
    return {"periodo": periodo, "dias_por_dia_da_semana": dias, "grade": grade}

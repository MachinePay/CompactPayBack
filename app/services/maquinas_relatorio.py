from collections import defaultdict
from datetime import date, datetime, timedelta
import re

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, aliased

from app.models.models import (
    AuditoriaOperacao,
    EventoTipo,
    FechamentoMaquina,
    HistoricoOperacao,
    Maquina,
    MetodoPagamento,
    Transacao,
    VendaPagamento,
)
from app.services.mercado_pago import get_active_terminal_for_machine
from app.services.pagamentos_helpers import should_allow_refund

OTA_TIMEOUT = timedelta(minutes=3)
OTA_ACTIVE_STATUSES = {"sent", "downloading", "restarting"}


def transacao_tipo_in_filter():
    return Transacao.tipo.in_([EventoTipo.in_flux, EventoTipo.in_flux.name, EventoTipo.in_flux.value, "IN"])


def transacao_tipo_out_filter():
    return Transacao.tipo.in_([EventoTipo.out_flux, EventoTipo.out_flux.name, EventoTipo.out_flux.value, "OUT"])


def transacao_metodo_fisico_filter():
    return Transacao.metodo.in_([MetodoPagamento.fisico, MetodoPagamento.fisico.name, MetodoPagamento.fisico.value, "FISICO"])


def transacao_tipo_value(tipo) -> str:
    value = getattr(tipo, "value", tipo)
    if value in {EventoTipo.in_flux.name, EventoTipo.in_flux.value, "IN"}:
        return "IN"
    if value in {EventoTipo.out_flux.name, EventoTipo.out_flux.value, "OUT"}:
        return "OUT"
    return str(value or "")


def apply_transacao_periodo(
    query,
    periodo: str | None = None,
    data_inicio: str | None = None,
    data_fim: str | None = None,
):
    if data_inicio and data_fim:
        dt_inicio, dt_fim = resolve_date_window(periodo, data_inicio, data_fim)
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


def resolve_date_window(
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


def real_payment_history_query(db: Session, machine_ids: list[str], start_dt: datetime, end_dt: datetime):
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


def real_revenue_totals(db: Session, machine_ids: list[str], start_dt: datetime, end_dt: datetime) -> tuple[float, int]:
    breakdown = real_revenue_breakdown(db, machine_ids, start_dt, end_dt)
    return float(breakdown["total"]), int(breakdown["count"])


def real_revenue_breakdown(db: Session, machine_ids: list[str], start_dt: datetime, end_dt: datetime) -> dict:
    if not machine_ids:
        return {"total": 0.0, "digital": 0.0, "fisico": 0.0, "count": 0}

    vendas_query = db.query(VendaPagamento).filter(
        VendaPagamento.maquina_id.in_(machine_ids),
        VendaPagamento.created_at >= start_dt,
        VendaPagamento.created_at <= end_dt,
        VendaPagamento.conta_faturamento.is_(True),
    )
    vendas_total = float(vendas_query.with_entities(func.sum(VendaPagamento.valor_liquido)).scalar() or 0.0)
    vendas_fisicas = float(
        vendas_query.filter(or_(VendaPagamento.origem == "fisico", VendaPagamento.provider == "fisico"))
        .with_entities(func.sum(VendaPagamento.valor_liquido))
        .scalar()
        or 0.0
    )
    vendas_digitais = max(0.0, vendas_total - vendas_fisicas)
    vendas_count = (
        vendas_query.filter(VendaPagamento.conta_ticket_medio.is_(True))
        .with_entities(func.count(VendaPagamento.id))
        .scalar()
        or 0
    )

    historicos_com_venda = db.query(VendaPagamento.historico_id).filter(VendaPagamento.historico_id.isnot(None))
    digital_legado_query = real_payment_history_query(db, machine_ids, start_dt, end_dt).filter(
        ~HistoricoOperacao.id.in_(historicos_com_venda)
    )
    digital_legado = float(digital_legado_query.with_entities(func.sum(HistoricoOperacao.valor)).scalar() or 0.0)
    digital_legado_count = digital_legado_query.with_entities(func.count(HistoricoOperacao.id)).scalar() or 0

    transacoes_com_venda = db.query(VendaPagamento.transacao_id).filter(VendaPagamento.transacao_id.isnot(None))
    fisico_legado_query = db.query(Transacao).filter(
        Transacao.maquina_id.in_(machine_ids),
        transacao_tipo_in_filter(),
        transacao_metodo_fisico_filter(),
        Transacao.data_hora >= start_dt,
        Transacao.data_hora <= end_dt,
        ~Transacao.id.in_(transacoes_com_venda),
    )
    fisico_legado = float(fisico_legado_query.with_entities(func.sum(Transacao.valor)).scalar() or 0.0)
    fisico_legado_count = fisico_legado_query.with_entities(func.count(Transacao.id)).scalar() or 0

    total_digital = vendas_digitais + digital_legado
    total_fisico = vendas_fisicas + fisico_legado
    return {
        "total": total_digital + total_fisico,
        "digital": total_digital,
        "fisico": total_fisico,
        "count": int(vendas_count or 0) + int(digital_legado_count or 0) + int(fisico_legado_count or 0),
    }


def compute_financial_summary(db: Session, machine_ids: list[str], start_dt: datetime, end_dt: datetime) -> dict:
    breakdown = real_revenue_breakdown(db, machine_ids, start_dt, end_dt)
    zero_summary = {
        "faturamento_total": 0.0,
        "faturamento_fisico": 0.0,
        "faturamento_digital": 0.0,
        "ticket_medio": 0.0,
        "vendas_count": 0,
        "testes_count": 0,
        "testes_valor": 0.0,
        "estornos_count": 0,
        "estornos_valor": 0.0,
        "pulsos_ausentes": 0,
    }
    if not machine_ids:
        return zero_summary

    # Creditos de teste (botao "Credito" de teste) sao gravados so em HistoricoOperacao
    # (categoria=TESTE), nunca em VendaPagamento - por isso a contagem vem de la.
    testes_query = db.query(HistoricoOperacao).filter(
        HistoricoOperacao.maquina_id.in_(machine_ids),
        HistoricoOperacao.categoria == "TESTE",
        HistoricoOperacao.created_at >= start_dt,
        HistoricoOperacao.created_at <= end_dt,
    )
    testes_count = testes_query.with_entities(func.count(HistoricoOperacao.id)).scalar() or 0
    testes_valor = float(testes_query.with_entities(func.sum(HistoricoOperacao.valor)).scalar() or 0.0)

    estornos_query = db.query(VendaPagamento).filter(
        VendaPagamento.maquina_id.in_(machine_ids),
        VendaPagamento.refunded_at.isnot(None),
        VendaPagamento.refunded_at >= start_dt,
        VendaPagamento.refunded_at <= end_dt,
    )
    estornos_count = estornos_query.with_entities(func.count(VendaPagamento.id)).scalar() or 0
    estornos_valor = float(estornos_query.with_entities(func.sum(VendaPagamento.valor_liquido)).scalar() or 0.0)

    pulsos_ausentes = (
        db.query(func.count(VendaPagamento.id))
        .filter(
            VendaPagamento.maquina_id.in_(machine_ids),
            VendaPagamento.created_at >= start_dt,
            VendaPagamento.created_at <= end_dt,
            VendaPagamento.status_pulso.in_(PULSE_ABSENT_STATUSES),
        )
        .scalar()
        or 0
    )

    vendas_count = int(breakdown["count"])
    return {
        "faturamento_total": float(breakdown["total"]),
        "faturamento_fisico": float(breakdown["fisico"]),
        "faturamento_digital": float(breakdown["digital"]),
        "ticket_medio": float(breakdown["total"]) / vendas_count if vendas_count else 0.0,
        "vendas_count": vendas_count,
        "testes_count": int(testes_count),
        "testes_valor": testes_valor,
        "estornos_count": int(estornos_count),
        "estornos_valor": estornos_valor,
        "pulsos_ausentes": int(pulsos_ausentes),
    }


def _zero_financial_summary() -> dict:
    return {
        "faturamento_total": 0.0,
        "faturamento_fisico": 0.0,
        "faturamento_digital": 0.0,
        "ticket_medio": 0.0,
        "vendas_count": 0,
        "testes_count": 0,
        "testes_valor": 0.0,
        "estornos_count": 0,
        "estornos_valor": 0.0,
        "pulsos_ausentes": 0,
    }


def compute_financial_summary_by_machine(
    db: Session, machine_ids: list[str], start_dt: datetime, end_dt: datetime
) -> dict[str, dict]:
    """Mesma quebra de compute_financial_summary, mas para todas as maquinas de uma
    vez. Busca cada fonte de dado em uma unica query (em vez de uma query por
    maquina) e soma tudo em memoria, evitando N+1 quando ha muitas maquinas."""
    if not machine_ids:
        return {}

    raw = {
        machine_id: {
            "digital": 0.0,
            "fisico": 0.0,
            "count": 0,
            "testes_count": 0,
            "testes_valor": 0.0,
            "estornos_count": 0,
            "estornos_valor": 0.0,
            "pulsos_ausentes": 0,
        }
        for machine_id in machine_ids
    }

    vendas_rows = (
        db.query(
            VendaPagamento.maquina_id,
            VendaPagamento.origem,
            VendaPagamento.provider,
            VendaPagamento.valor_liquido,
            VendaPagamento.conta_ticket_medio,
        )
        .filter(
            VendaPagamento.maquina_id.in_(machine_ids),
            VendaPagamento.created_at >= start_dt,
            VendaPagamento.created_at <= end_dt,
            VendaPagamento.conta_faturamento.is_(True),
        )
        .all()
    )
    for maquina_id, origem, provider, valor_liquido, conta_ticket_medio in vendas_rows:
        bucket = raw[maquina_id]
        valor = float(valor_liquido or 0.0)
        if origem == "fisico" or provider == "fisico":
            bucket["fisico"] += valor
        else:
            bucket["digital"] += valor
        if conta_ticket_medio:
            bucket["count"] += 1

    historicos_com_venda = db.query(VendaPagamento.historico_id).filter(VendaPagamento.historico_id.isnot(None))
    digital_legado_rows = (
        real_payment_history_query(db, machine_ids, start_dt, end_dt)
        .filter(~HistoricoOperacao.id.in_(historicos_com_venda))
        .with_entities(HistoricoOperacao.maquina_id, HistoricoOperacao.valor)
        .all()
    )
    for maquina_id, valor in digital_legado_rows:
        bucket = raw.get(maquina_id)
        if bucket is None:
            continue
        bucket["digital"] += float(valor or 0.0)
        bucket["count"] += 1

    transacoes_com_venda = db.query(VendaPagamento.transacao_id).filter(VendaPagamento.transacao_id.isnot(None))
    fisico_legado_rows = (
        db.query(Transacao.maquina_id, Transacao.valor)
        .filter(
            Transacao.maquina_id.in_(machine_ids),
            transacao_tipo_in_filter(),
            transacao_metodo_fisico_filter(),
            Transacao.data_hora >= start_dt,
            Transacao.data_hora <= end_dt,
            ~Transacao.id.in_(transacoes_com_venda),
        )
        .all()
    )
    for maquina_id, valor in fisico_legado_rows:
        bucket = raw.get(maquina_id)
        if bucket is None:
            continue
        bucket["fisico"] += float(valor or 0.0)
        bucket["count"] += 1

    testes_rows = (
        db.query(HistoricoOperacao.maquina_id, HistoricoOperacao.valor)
        .filter(
            HistoricoOperacao.maquina_id.in_(machine_ids),
            HistoricoOperacao.categoria == "TESTE",
            HistoricoOperacao.created_at >= start_dt,
            HistoricoOperacao.created_at <= end_dt,
        )
        .all()
    )
    for maquina_id, valor in testes_rows:
        bucket = raw[maquina_id]
        bucket["testes_count"] += 1
        bucket["testes_valor"] += float(valor or 0.0)

    estornos_rows = (
        db.query(VendaPagamento.maquina_id, VendaPagamento.valor_liquido)
        .filter(
            VendaPagamento.maquina_id.in_(machine_ids),
            VendaPagamento.refunded_at.isnot(None),
            VendaPagamento.refunded_at >= start_dt,
            VendaPagamento.refunded_at <= end_dt,
        )
        .all()
    )
    for maquina_id, valor_liquido in estornos_rows:
        bucket = raw[maquina_id]
        bucket["estornos_count"] += 1
        bucket["estornos_valor"] += float(valor_liquido or 0.0)

    pulsos_rows = (
        db.query(VendaPagamento.maquina_id)
        .filter(
            VendaPagamento.maquina_id.in_(machine_ids),
            VendaPagamento.created_at >= start_dt,
            VendaPagamento.created_at <= end_dt,
            VendaPagamento.status_pulso.in_(PULSE_ABSENT_STATUSES),
        )
        .all()
    )
    for (maquina_id,) in pulsos_rows:
        raw[maquina_id]["pulsos_ausentes"] += 1

    result = {}
    for machine_id, bucket in raw.items():
        # O bucket "digital" e somado diretamente (nao por subtracao), entao um
        # valor_liquido negativo isolado nao pode deixar o total negativo aqui -
        # mantem o mesmo espirito do clamp em real_revenue_breakdown.
        digital = max(0.0, bucket["digital"])
        total = digital + bucket["fisico"]
        summary = _zero_financial_summary()
        summary["faturamento_total"] = total
        summary["faturamento_fisico"] = bucket["fisico"]
        summary["faturamento_digital"] = digital
        summary["vendas_count"] = bucket["count"]
        summary["ticket_medio"] = total / bucket["count"] if bucket["count"] else 0.0
        summary["testes_count"] = bucket["testes_count"]
        summary["testes_valor"] = bucket["testes_valor"]
        summary["estornos_count"] = bucket["estornos_count"]
        summary["estornos_valor"] = bucket["estornos_valor"]
        summary["pulsos_ausentes"] = bucket["pulsos_ausentes"]
        result[machine_id] = summary
    return result


def sum_financial_summaries(summaries: list[dict]) -> dict:
    """Combina varios resumos (ex.: das maquinas de um cliente) em um so total.
    Ticket medio nao e uma media das medias - e recalculado a partir dos totais
    somados, senao maquinas com poucas vendas distorceriam o resultado."""
    combined = _zero_financial_summary()
    for summary in summaries:
        combined["faturamento_total"] += summary["faturamento_total"]
        combined["faturamento_fisico"] += summary["faturamento_fisico"]
        combined["faturamento_digital"] += summary["faturamento_digital"]
        combined["vendas_count"] += summary["vendas_count"]
        combined["testes_count"] += summary["testes_count"]
        combined["testes_valor"] += summary["testes_valor"]
        combined["estornos_count"] += summary["estornos_count"]
        combined["estornos_valor"] += summary["estornos_valor"]
        combined["pulsos_ausentes"] += summary["pulsos_ausentes"]
    combined["ticket_medio"] = (
        combined["faturamento_total"] / combined["vendas_count"] if combined["vendas_count"] else 0.0
    )
    return combined


def latest_activity_by_machine(db: Session, machine_ids: list[str]) -> dict:
    if not machine_ids:
        return {}
    rows = (
        db.query(Transacao.maquina_id, func.max(Transacao.data_hora))
        .filter(Transacao.maquina_id.in_(machine_ids))
        .group_by(Transacao.maquina_id)
        .all()
    )
    return dict(rows)


def movement_counts_by_machine(db: Session, machine_ids: list[str], start_dt: datetime, end_dt: datetime) -> dict:
    if not machine_ids:
        return {}
    rows = (
        db.query(Transacao.maquina_id, func.count(Transacao.id))
        .filter(
            Transacao.maquina_id.in_(machine_ids),
            Transacao.data_hora >= start_dt,
            Transacao.data_hora <= end_dt,
        )
        .group_by(Transacao.maquina_id)
        .all()
    )
    return dict(rows)


def latest_payment_by_machine(db: Session, machine_ids: list[str]) -> dict[str, dict]:
    """Ultimo VendaPagamento de cada maquina, buscando tudo numa unica query (via
    ROW_NUMBER) em vez de uma consulta 'ORDER BY ... LIMIT 1' por maquina."""
    if not machine_ids:
        return {}
    row_number = (
        func.row_number()
        .over(partition_by=VendaPagamento.maquina_id, order_by=VendaPagamento.created_at.desc())
        .label("rn")
    )
    subq = (
        db.query(VendaPagamento, row_number)
        .filter(VendaPagamento.maquina_id.in_(machine_ids))
        .subquery()
    )
    venda_alias = aliased(VendaPagamento, subq)
    rows = db.query(venda_alias).filter(subq.c.rn == 1).all()
    result = {}
    for venda in rows:
        result[venda.maquina_id] = {
            "data": venda.created_at,
            "valor": float(venda.valor_liquido or venda.valor_bruto or 0),
            "origem": venda.origem,
            "provider": venda.provider,
            "payment_type": venda.tipo_pagamento,
            "pulse_status": venda.status_pulso,
            "is_teste": bool(venda.is_teste),
        }
    return result


def latest_transacao_in_by_machine(db: Session, machine_ids: list[str]) -> dict[str, dict]:
    """Fallback do faturamento legado (Transacao IN) para maquinas sem nenhuma
    VendaPagamento ainda, tambem buscado em lote."""
    if not machine_ids:
        return {}
    row_number = (
        func.row_number()
        .over(partition_by=Transacao.maquina_id, order_by=Transacao.data_hora.desc())
        .label("rn")
    )
    subq = (
        db.query(Transacao, row_number)
        .filter(Transacao.maquina_id.in_(machine_ids), transacao_tipo_in_filter())
        .subquery()
    )
    transacao_alias = aliased(Transacao, subq)
    rows = db.query(transacao_alias).filter(subq.c.rn == 1).all()
    result = {}
    for transacao in rows:
        metodo = transacao.metodo.value if hasattr(transacao.metodo, "value") else str(transacao.metodo)
        result[transacao.maquina_id] = {
            "data": transacao.data_hora,
            "valor": float(transacao.valor or 0),
            "origem": metodo.lower(),
            "provider": metodo.lower(),
            "payment_type": metodo,
            "pulse_status": "fisico",
            "is_teste": False,
        }
    return result


def latest_pulse_by_machine(db: Session, machine_ids: list[str]) -> dict[str, dict]:
    if not machine_ids:
        return {}
    row_number = (
        func.row_number()
        .over(partition_by=HistoricoOperacao.maquina_id, order_by=HistoricoOperacao.created_at.desc())
        .label("rn")
    )
    subq = (
        db.query(HistoricoOperacao, row_number)
        .filter(
            HistoricoOperacao.maquina_id.in_(machine_ids),
            HistoricoOperacao.pulse_status.isnot(None),
        )
        .subquery()
    )
    historico_alias = aliased(HistoricoOperacao, subq)
    rows = db.query(historico_alias).filter(subq.c.rn == 1).all()
    result = {}
    for historico in rows:
        result[historico.maquina_id] = {
            "data": historico.created_at,
            "status": historico.pulse_status,
            "categoria": historico.categoria,
            "descricao": historico.descricao,
            "command_id": historico.command_id,
        }
    return result


def noise_counts_by_machine(db: Session, machine_ids: list[str], since: datetime) -> dict[str, int]:
    if not machine_ids:
        return {}
    rows = (
        db.query(HistoricoOperacao.maquina_id, func.count(HistoricoOperacao.id))
        .filter(
            HistoricoOperacao.maquina_id.in_(machine_ids),
            HistoricoOperacao.categoria == "DISPOSITIVO",
            HistoricoOperacao.created_at >= since,
            or_(
                HistoricoOperacao.descricao.ilike("%PULSE_CURTO%"),
                HistoricoOperacao.descricao.ilike("%CURTO_IGNORADO%"),
                HistoricoOperacao.descricao.ilike("%COIN_RETURN_IGNORADO%"),
            ),
        )
        .group_by(HistoricoOperacao.maquina_id)
        .all()
    )
    return dict(rows)


OFFLINE_ALERT_AFTER = timedelta(minutes=5)
NO_PAYMENT_ALERT_AFTER = timedelta(days=7)
NOISE_ALERT_WINDOW = timedelta(hours=24)
NOISE_ALERT_THRESHOLD = 10


def latest_payment_map(db: Session, machine_ids: list[str]) -> dict[str, dict]:
    pagamentos = latest_payment_by_machine(db, machine_ids)
    legado = latest_transacao_in_by_machine(db, machine_ids)
    return {machine_id: pagamentos.get(machine_id) or legado.get(machine_id) for machine_id in machine_ids}


def wifi_health(quality) -> str:
    if quality is None:
        return "sem_leitura"
    if quality >= 70:
        return "otimo"
    if quality >= 40:
        return "bom"
    return "ruim"


def machine_health_status(status_online: bool, wifi_status: str, firmware_alert: bool, pulse_alert: bool) -> str:
    if not status_online:
        return "offline"
    if wifi_status == "ruim" or firmware_alert or pulse_alert:
        return "atencao"
    return "online"


def serialize_health_machine(maquina: Maquina, now: datetime, ultimo_pagamento: dict | None, ultimo_pulso: dict | None) -> dict:
    status_online = bool(maquina.ultimo_sinal and (now - maquina.ultimo_sinal) < ONLINE_SIGNAL_WINDOW)
    wifi_status = wifi_health(maquina.wifi_quality)
    firmware_update_status = maquina.firmware_update_status or ""
    firmware_alert = (
        firmware_update_status in OTA_ACTIVE_STATUSES
        or firmware_update_status == "failed"
        or bool(maquina.firmware_target_version and maquina.firmware_target_version != maquina.firmware_version)
    )
    pulse_status = str((ultimo_pulso or {}).get("status") or "").lower()
    pulse_alert = pulse_status.startswith("falha") or pulse_status in PULSE_ABSENT_STATUSES
    health_status = machine_health_status(status_online, wifi_status, firmware_alert, pulse_alert)

    return {
        "id_hardware": maquina.id_hardware,
        "cliente_id": maquina.cliente_id,
        "cliente_nome": maquina.dono.nome_empresa if getattr(maquina, "dono", None) else None,
        "nome": maquina.nome_local,
        "localizacao": maquina.localizacao,
        "health_status": health_status,
        "status_online": status_online,
        "mqtt_status": "conectado" if status_online else "sem_sinal",
        "ultimo_sinal": maquina.ultimo_sinal,
        "wifi_quality": maquina.wifi_quality,
        "wifi_rssi": maquina.wifi_rssi,
        "wifi_status": wifi_status,
        "firmware_version": maquina.firmware_version,
        "firmware_target_version": maquina.firmware_target_version,
        "firmware_update_status": firmware_update_status,
        "firmware_alert": firmware_alert,
        "ultimo_pagamento": ultimo_pagamento,
        "ultimo_pulso": ultimo_pulso,
        "pulse_alert": pulse_alert,
        "uptime_seconds": maquina.uptime_seconds,
        "free_heap_bytes": maquina.free_heap_bytes,
        "last_reset_reason": maquina.last_reset_reason,
        "wifi_reconnect_count": maquina.wifi_reconnect_count,
        "mqtt_reconnect_count": maquina.mqtt_reconnect_count,
        "short_pulse_count": maquina.short_pulse_count,
    }


def compute_all_machines_health(db: Session, maquinas: list[Maquina], now: datetime | None = None) -> list[dict]:
    """Serializa a saude de todas as maquinas buscando os dados em lote (evita
    N+1 quando ha muitas maquinas)."""
    now = now or datetime.utcnow()
    machine_ids = [maquina.id_hardware for maquina in maquinas]
    pagamentos_por_maquina = latest_payment_map(db, machine_ids)
    pulsos_por_maquina = latest_pulse_by_machine(db, machine_ids)
    return [
        serialize_health_machine(
            maquina, now, pagamentos_por_maquina.get(maquina.id_hardware), pulsos_por_maquina.get(maquina.id_hardware)
        )
        for maquina in maquinas
    ]


def make_alert(machine: dict, tipo: str, severidade: str, titulo: str, mensagem: str, detected_at, extra: dict | None = None) -> dict:
    return {
        "id": f"{machine['id_hardware']}:{tipo}",
        "tipo": tipo,
        "severidade": severidade,
        "titulo": titulo,
        "mensagem": mensagem,
        "detected_at": detected_at,
        "maquina": {
            "id_hardware": machine["id_hardware"],
            "nome": machine["nome"],
            "cliente_nome": machine["cliente_nome"],
            "localizacao": machine["localizacao"],
        },
        "extra": extra or {},
    }


def build_machine_alerts(machine: dict, now: datetime, noise_count: int) -> list[dict]:
    alerts = []
    last_signal = machine.get("ultimo_sinal")
    if not machine["status_online"] and last_signal and now - last_signal >= OFFLINE_ALERT_AFTER:
        minutes = int((now - last_signal).total_seconds() // 60)
        alerts.append(
            make_alert(
                machine,
                "offline",
                "critico",
                "Maquina offline",
                f"Sem sinal ha {minutes} minuto(s).",
                last_signal,
                {"offline_minutos": minutes},
            )
        )

    if machine["wifi_status"] == "ruim":
        alerts.append(
            make_alert(
                machine,
                "wifi_ruim",
                "aviso",
                "Wi-Fi ruim",
                f"Sinal em {machine.get('wifi_quality')}% ({machine.get('wifi_rssi')} dBm).",
                last_signal,
                {"wifi_quality": machine.get("wifi_quality"), "wifi_rssi": machine.get("wifi_rssi")},
            )
        )

    pulse = machine.get("ultimo_pulso")
    pulse_status = str((pulse or {}).get("status") or "").lower()
    if machine["pulse_alert"] and pulse:
        alerts.append(
            make_alert(
                machine,
                "pulso_ausente",
                "critico",
                "Pagamento com pulso ausente",
                f"Ultimo pulso registrado como {pulse_status}.",
                pulse.get("data"),
                {"pulse_status": pulse_status, "command_id": pulse.get("command_id")},
            )
        )

    if machine["firmware_alert"]:
        status = machine.get("firmware_update_status") or "pendente"
        severity = "critico" if status == "failed" else "aviso"
        alerts.append(
            make_alert(
                machine,
                "firmware",
                severity,
                "Firmware requer atencao",
                f"Versao atual {machine.get('firmware_version') or 'sem versao'}; alvo {machine.get('firmware_target_version') or 'nao definido'}; status {status}.",
                last_signal,
                {
                    "firmware_version": machine.get("firmware_version"),
                    "firmware_target_version": machine.get("firmware_target_version"),
                    "firmware_update_status": status,
                },
            )
        )

    payment = machine.get("ultimo_pagamento")
    if payment and payment.get("data") and now - payment["data"] >= NO_PAYMENT_ALERT_AFTER:
        days = int((now - payment["data"]).total_seconds() // 86400)
        alerts.append(
            make_alert(
                machine,
                "sem_pagamento_recente",
                "info",
                "Sem pagamento recente",
                f"Ultimo pagamento ha {days} dia(s).",
                payment["data"],
                {"dias": days, "valor": payment.get("valor")},
            )
        )

    if noise_count >= NOISE_ALERT_THRESHOLD:
        alerts.append(
            make_alert(
                machine,
                "ruido_contador",
                "aviso",
                "Ruido no contador",
                f"{noise_count} pulsos curtos/ignorados nas ultimas 24h.",
                now,
                {"eventos_24h": noise_count},
            )
        )

    return alerts


def compute_active_alerts(db: Session, maquinas: list[Maquina], now: datetime | None = None) -> list[dict]:
    """Todos os alertas ativos das maquinas informadas, buscando os dados em lote.
    Usado tanto pelo painel de alertas quanto pelo notificador em background."""
    now = now or datetime.utcnow()
    machine_ids = [maquina.id_hardware for maquina in maquinas]
    machines = compute_all_machines_health(db, maquinas, now)
    ruido_por_maquina = noise_counts_by_machine(db, machine_ids, now - NOISE_ALERT_WINDOW)
    alerts = []
    for machine in machines:
        alerts.extend(build_machine_alerts(machine, now, ruido_por_maquina.get(machine["id_hardware"], 0)))
    alerts.sort(key=lambda item: item.get("detected_at") or datetime.min, reverse=True)
    return alerts


def transacao_summary_by_machine(
    db: Session, machine_ids: list[str], start_dt: datetime, end_dt: datetime
) -> dict[str, dict]:
    """Para cada maquina: data do ultimo pagamento (IN), data da ultima saida (OUT)
    e quantidade de saidas no periodo - tudo numa unica query agrupada."""
    result = {
        machine_id: {"ultimo_pagamento_em": None, "ultima_saida_em": None, "quantidade_saidas": 0}
        for machine_id in machine_ids
    }
    if not machine_ids:
        return result

    rows = (
        db.query(
            Transacao.maquina_id,
            Transacao.tipo,
            func.max(Transacao.data_hora),
            func.count(Transacao.id),
        )
        .filter(
            Transacao.maquina_id.in_(machine_ids),
            Transacao.data_hora >= start_dt,
            Transacao.data_hora <= end_dt,
        )
        .group_by(Transacao.maquina_id, Transacao.tipo)
        .all()
    )
    for maquina_id, tipo, ultimo, count in rows:
        tipo_value = transacao_tipo_value(tipo)
        bucket = result[maquina_id]
        if tipo_value == "IN":
            bucket["ultimo_pagamento_em"] = ultimo
        elif tipo_value == "OUT":
            bucket["ultima_saida_em"] = ultimo
            bucket["quantidade_saidas"] = int(count or 0)
    return result


def latest_teste_at_by_machine(db: Session, machine_ids: list[str], start_dt: datetime, end_dt: datetime) -> dict:
    if not machine_ids:
        return {}
    rows = (
        db.query(HistoricoOperacao.maquina_id, func.max(HistoricoOperacao.created_at))
        .filter(
            HistoricoOperacao.maquina_id.in_(machine_ids),
            HistoricoOperacao.categoria == "TESTE",
            HistoricoOperacao.created_at >= start_dt,
            HistoricoOperacao.created_at <= end_dt,
        )
        .group_by(HistoricoOperacao.maquina_id)
        .all()
    )
    return dict(rows)


def daily_revenue_totals(db: Session, machine_ids: list[str], start_dt: datetime, end_dt: datetime) -> dict:
    """Faturamento real por dia, buscando cada fonte de dado uma unica vez para
    todo o periodo (em vez de uma consulta por dia)."""
    totals: dict = defaultdict(float)
    if not machine_ids:
        return totals

    vendas_rows = (
        db.query(VendaPagamento.created_at, VendaPagamento.valor_liquido)
        .filter(
            VendaPagamento.maquina_id.in_(machine_ids),
            VendaPagamento.created_at >= start_dt,
            VendaPagamento.created_at <= end_dt,
            VendaPagamento.conta_faturamento.is_(True),
        )
        .all()
    )
    for created_at, valor_liquido in vendas_rows:
        totals[created_at.date()] += float(valor_liquido or 0.0)

    historicos_com_venda = db.query(VendaPagamento.historico_id).filter(VendaPagamento.historico_id.isnot(None))
    digital_legado_rows = (
        real_payment_history_query(db, machine_ids, start_dt, end_dt)
        .filter(~HistoricoOperacao.id.in_(historicos_com_venda))
        .with_entities(HistoricoOperacao.created_at, HistoricoOperacao.valor)
        .all()
    )
    for created_at, valor in digital_legado_rows:
        totals[created_at.date()] += float(valor or 0.0)

    transacoes_com_venda = db.query(VendaPagamento.transacao_id).filter(VendaPagamento.transacao_id.isnot(None))
    fisico_legado_rows = (
        db.query(Transacao.data_hora, Transacao.valor)
        .filter(
            Transacao.maquina_id.in_(machine_ids),
            transacao_tipo_in_filter(),
            transacao_metodo_fisico_filter(),
            Transacao.data_hora >= start_dt,
            Transacao.data_hora <= end_dt,
            ~Transacao.id.in_(transacoes_com_venda),
        )
        .all()
    )
    for data_hora, valor in fisico_legado_rows:
        totals[data_hora.date()] += float(valor or 0.0)

    return totals


ONLINE_SIGNAL_WINDOW = timedelta(seconds=90)
TERMINAL_PAYMENT_ONLINE_WINDOW = timedelta(minutes=5)
PULSE_CONFIRMED_STATUSES = {
    "pulso_confirmado",
    "pulsos_confirmados",
    "pulso_enviado",
    "pulso_unitario",
    "liberado",
    "fisico",
}
PULSE_ABSENT_STATUSES = {
    "falha",
    "falha_timeout",
    "falha_sem_confirmacao",
    "falha_publicacao",
    "falha_cmd_ignorado",
    "falha_bloqueado",
    "pulso_sem_retorno",
}


def _normalize_filter(value: str | None, default: str = "todos") -> str:
    return (value or default).strip().lower()


def _payment_method_conditions(forma: str):
    if forma == "todos":
        return []
    if forma == "pix":
        patterns = ["%pix%"]
    elif forma == "cartao":
        patterns = [
            "%card%",
            "%cartao%",
            "%credito%",
            "%credit%",
            "%debito%",
            "%debit%",
            "%visa%",
            "%master%",
            "%elo%",
            "%amex%",
            "%hiper%",
        ]
    elif forma == "credito":
        patterns = ["%credit%", "%credito%"]
    elif forma == "debito":
        patterns = ["%debit%", "%debito%"]
    else:
        return []
    fields = [
        HistoricoOperacao.payment_type,
        HistoricoOperacao.card_brand,
        HistoricoOperacao.bank_name,
        HistoricoOperacao.provider,
    ]
    return [field.ilike(pattern) for field in fields for pattern in patterns]


def _apply_history_sale_filters(query, origem: str, forma: str, pulso: str, busca: str):
    if origem == "fisico":
        return query.filter(HistoricoOperacao.id.is_(None))
    if origem == "app_agarra":
        query = query.filter(
            or_(
                HistoricoOperacao.provider == "agarramais_app",
                HistoricoOperacao.payment_type == "pagamento_app_agarra",
                HistoricoOperacao.descricao.ilike("%aplicativo Agarra%"),
            )
        )

    if pulso == "confirmados":
        query = query.filter(
            or_(
                HistoricoOperacao.pulse_status.is_(None),
                HistoricoOperacao.pulse_status.in_(PULSE_CONFIRMED_STATUSES),
            )
        )
    elif pulso == "ausentes":
        query = query.filter(
            or_(
                HistoricoOperacao.pulse_status.in_(PULSE_ABSENT_STATUSES),
                HistoricoOperacao.pulse_status.ilike("falha%"),
            )
        )

    method_conditions = _payment_method_conditions(forma)
    if method_conditions:
        query = query.filter(or_(*method_conditions))

    if busca:
        search = f"%{busca}%"
        query = query.filter(
            or_(
                HistoricoOperacao.descricao.ilike(search),
                HistoricoOperacao.provider_payment_id.ilike(search),
                HistoricoOperacao.payment_type.ilike(search),
                HistoricoOperacao.card_brand.ilike(search),
                HistoricoOperacao.bank_name.ilike(search),
                HistoricoOperacao.provider.ilike(search),
                HistoricoOperacao.pulse_status.ilike(search),
            )
        )
    return query


def _should_include_tests(registro: str, origem: str, forma: str, pulso: str, busca: str) -> bool:
    if registro == "reais":
        return False
    if origem != "todos" or forma != "todos" or pulso != "todos":
        return False
    return True


def _should_include_physical_sales(registro: str, origem: str, forma: str, pulso: str, busca: str) -> bool:
    if registro == "testes" or origem in {"digital", "app_agarra"} or forma != "todos" or pulso == "ausentes":
        return False
    if busca and not any(term in busca for term in ["fisico", "físico", "pagamento", "maquina", "máquina"]):
        return False
    return True


def recent_terminal_payment_status(db: Session, machine_id: str) -> dict:
    terminal_payment = (
        db.query(HistoricoOperacao)
        .filter(
            HistoricoOperacao.maquina_id == machine_id,
            HistoricoOperacao.categoria == "PAGAMENTO",
            HistoricoOperacao.descricao.ilike("%terminal_id=%"),
        )
        .order_by(HistoricoOperacao.created_at.desc())
        .first()
    )
    if not terminal_payment:
        return {"online": False, "terminal_id": None, "last_payment_at": None}

    match = re.search(
        r"terminal_id=([^,\)\s]+)",
        terminal_payment.descricao or "",
        flags=re.IGNORECASE,
    )
    terminal_id = match.group(1) if match else None
    is_recent = bool(
        terminal_payment.created_at
        and datetime.utcnow() - terminal_payment.created_at < TERMINAL_PAYMENT_ONLINE_WINDOW
    )
    return {
        "online": is_recent,
        "terminal_id": terminal_id,
        "last_payment_at": terminal_payment.created_at,
    }


def status_operacional(status_online: bool, ultima_atividade_em: datetime | None) -> str:
    if not status_online:
        return "offline"
    if ultima_atividade_em is None:
        return "atencao"
    return "operando"


def serialize_machine_summary(
    db: Session,
    maquina: Maquina,
    periodo: str = "mes",
    data_inicio: str = None,
    data_fim: str = None,
):
    agora = datetime.utcnow()
    status_online = bool(maquina.ultimo_sinal and (agora - maquina.ultimo_sinal) < ONLINE_SIGNAL_WINDOW)
    start_dt, end_dt = resolve_date_window(periodo, data_inicio, data_fim)
    faturamento, _ = real_revenue_totals(db, [maquina.id_hardware], start_dt, end_dt)
    ultimo_pagamento_em = (
        db.query(func.max(Transacao.data_hora))
        .filter(
            Transacao.maquina_id == maquina.id_hardware,
            transacao_tipo_in_filter(),
            Transacao.data_hora >= start_dt,
            Transacao.data_hora <= end_dt,
        )
        .scalar()
    )
    ultima_saida_em = (
        db.query(func.max(Transacao.data_hora))
        .filter(
            Transacao.maquina_id == maquina.id_hardware,
            transacao_tipo_out_filter(),
            Transacao.data_hora >= start_dt,
            Transacao.data_hora <= end_dt,
        )
        .scalar()
    )
    quantidade_saidas = (
        db.query(func.count(Transacao.id))
        .filter(
            Transacao.maquina_id == maquina.id_hardware,
            transacao_tipo_out_filter(),
            Transacao.data_hora >= start_dt,
            Transacao.data_hora <= end_dt,
        )
        .scalar()
        or 0
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
    firmware_update_status = maquina.firmware_update_status
    update_started_at = maquina.firmware_update_started_at or maquina.firmware_update_requested_at
    if (
        firmware_update_status in OTA_ACTIVE_STATUSES
        and update_started_at
        and agora - update_started_at > OTA_TIMEOUT
    ):
        firmware_update_status = "failed"
        maquina.firmware_update_status = "failed"
        maquina.firmware_update_finished_at = agora
        db.commit()

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
        "firmware_version": maquina.firmware_version,
        "firmware_target_version": maquina.firmware_target_version,
        "firmware_updated_at": maquina.firmware_updated_at,
        "firmware_update_status": firmware_update_status,
        "firmware_update_command_id": maquina.firmware_update_command_id,
        "firmware_update_url": maquina.firmware_update_url,
        "firmware_update_requested_at": maquina.firmware_update_requested_at,
        "firmware_update_started_at": maquina.firmware_update_started_at,
        "firmware_update_finished_at": maquina.firmware_update_finished_at,
        "ultimo_sinal": maquina.ultimo_sinal,
        "wifi_rssi": maquina.wifi_rssi,
        "wifi_quality": maquina.wifi_quality,
        "ultimo_pagamento_em": ultimo_pagamento_em,
        "ultimo_teste_em": ultimo_teste_em,
        "ultima_saida_em": ultima_saida_em,
        "ultima_atividade_em": ultima_atividade_em,
        "status_online": status_online,
        "status_operacional": status_operacional(status_online, ultima_atividade_em),
        "faturamento": float(faturamento),
        "quantidade_saidas": int(quantidade_saidas),
    }


def serialize_machines_summary_batch(
    db: Session,
    maquinas: list[Maquina],
    periodo: str = "mes",
    data_inicio: str = None,
    data_fim: str = None,
) -> list[dict]:
    """Mesmo resultado de chamar serialize_machine_summary maquina por maquina, mas
    buscando os dados de todas de uma vez (poucas queries agregadas) - usado pela
    listagem principal de maquinas, que e a mais consultada do sistema."""
    agora = datetime.utcnow()
    start_dt, end_dt = resolve_date_window(periodo, data_inicio, data_fim)
    machine_ids = [maquina.id_hardware for maquina in maquinas]

    faturamento_por_maquina = compute_financial_summary_by_machine(db, machine_ids, start_dt, end_dt)
    transacoes_por_maquina = transacao_summary_by_machine(db, machine_ids, start_dt, end_dt)
    testes_por_maquina = latest_teste_at_by_machine(db, machine_ids, start_dt, end_dt)

    houve_commit_pendente = False
    resultado = []
    for maquina in maquinas:
        status_online = bool(maquina.ultimo_sinal and (agora - maquina.ultimo_sinal) < ONLINE_SIGNAL_WINDOW)
        transacoes = transacoes_por_maquina.get(
            maquina.id_hardware, {"ultimo_pagamento_em": None, "ultima_saida_em": None, "quantidade_saidas": 0}
        )
        ultimo_pagamento_em = transacoes["ultimo_pagamento_em"]
        ultima_saida_em = transacoes["ultima_saida_em"]
        quantidade_saidas = transacoes["quantidade_saidas"]
        ultimo_teste_em = testes_por_maquina.get(maquina.id_hardware)
        ultima_atividade_em = max(
            [item for item in [ultimo_pagamento_em, ultima_saida_em, ultimo_teste_em] if item is not None],
            default=None,
        )

        firmware_update_status = maquina.firmware_update_status
        update_started_at = maquina.firmware_update_started_at or maquina.firmware_update_requested_at
        if (
            firmware_update_status in OTA_ACTIVE_STATUSES
            and update_started_at
            and agora - update_started_at > OTA_TIMEOUT
        ):
            firmware_update_status = "failed"
            maquina.firmware_update_status = "failed"
            maquina.firmware_update_finished_at = agora
            houve_commit_pendente = True

        faturamento = faturamento_por_maquina.get(maquina.id_hardware, {}).get("faturamento_total", 0.0)

        resultado.append(
            {
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
                "firmware_version": maquina.firmware_version,
                "firmware_target_version": maquina.firmware_target_version,
                "firmware_updated_at": maquina.firmware_updated_at,
                "firmware_update_status": firmware_update_status,
                "firmware_update_command_id": maquina.firmware_update_command_id,
                "firmware_update_url": maquina.firmware_update_url,
                "firmware_update_requested_at": maquina.firmware_update_requested_at,
                "firmware_update_started_at": maquina.firmware_update_started_at,
                "firmware_update_finished_at": maquina.firmware_update_finished_at,
                "ultimo_sinal": maquina.ultimo_sinal,
                "wifi_rssi": maquina.wifi_rssi,
                "wifi_quality": maquina.wifi_quality,
                "ultimo_pagamento_em": ultimo_pagamento_em,
                "ultimo_teste_em": ultimo_teste_em,
                "ultima_saida_em": ultima_saida_em,
                "ultima_atividade_em": ultima_atividade_em,
                "status_online": status_online,
                "status_operacional": status_operacional(status_online, ultima_atividade_em),
                "faturamento": float(faturamento),
                "quantidade_saidas": int(quantidade_saidas),
            }
        )

    if houve_commit_pendente:
        db.commit()

    return resultado


def build_machine_history_payload(
    db: Session,
    maquina: Maquina,
    periodo: str = "mes",
    data_inicio: str = None,
    data_fim: str = None,
    registro: str = "todos",
    origem: str = "todos",
    forma: str = "todos",
    pulso: str = "todos",
    busca: str = "",
):
    machine_id = maquina.id_hardware
    registro_filter = _normalize_filter(registro)
    origem_filter = _normalize_filter(origem)
    forma_filter = _normalize_filter(forma)
    pulso_filter = _normalize_filter(pulso)
    busca_filter = (busca or "").strip().lower()
    transacoes_query = db.query(Transacao).filter(Transacao.maquina_id == machine_id)
    transacoes_query = apply_transacao_periodo(
        transacoes_query,
        periodo=periodo,
        data_inicio=data_inicio,
        data_fim=data_fim,
    )

    pagamentos = transacoes_query.filter(transacao_tipo_in_filter()).order_by(Transacao.data_hora.desc()).all()
    saidas = transacoes_query.filter(transacao_tipo_out_filter()).order_by(Transacao.data_hora.desc()).all()

    start_dt, end_dt = resolve_date_window(periodo, data_inicio, data_fim)
    testes_query = db.query(HistoricoOperacao).filter(
        HistoricoOperacao.maquina_id == machine_id,
        HistoricoOperacao.categoria == "TESTE",
        HistoricoOperacao.created_at >= start_dt,
        HistoricoOperacao.created_at <= end_dt,
    )
    if busca_filter and "teste" not in busca_filter:
        testes_query = testes_query.filter(HistoricoOperacao.descricao.ilike(f"%{busca_filter}%"))
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
    testes_vendas = (
        testes_query.order_by(HistoricoOperacao.created_at.desc()).all()
        if _should_include_tests(registro_filter, origem_filter, forma_filter, pulso_filter, busca_filter)
        else []
    )
    pagamentos_historico_query = (
        db.query(HistoricoOperacao)
        .filter(
            HistoricoOperacao.maquina_id == machine_id,
            HistoricoOperacao.categoria == "PAGAMENTO",
            HistoricoOperacao.created_at >= start_dt,
            HistoricoOperacao.created_at <= end_dt,
        )
    )
    pagamentos_historico = (
        _apply_history_sale_filters(
            pagamentos_historico_query,
            origem_filter,
            forma_filter,
            pulso_filter,
            busca_filter,
        )
        .order_by(HistoricoOperacao.created_at.desc())
        .all()
        if registro_filter != "testes"
        else []
    )

    resumo_faturamento = real_revenue_breakdown(db, [machine_id], start_dt, end_dt)
    total_pagamentos = resumo_faturamento["total"]
    total_digital = resumo_faturamento["digital"]
    total_fisico = resumo_faturamento["fisico"]
    quantidade_pagamentos_reais = resumo_faturamento["count"]
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
        .filter(HistoricoOperacao.maquina_id == machine_id, HistoricoOperacao.categoria == "MANUTENCAO")
        .order_by(HistoricoOperacao.created_at.desc())
        .limit(20)
        .all()
    )
    eventos_dispositivo = (
        db.query(HistoricoOperacao)
        .filter(HistoricoOperacao.maquina_id == machine_id, HistoricoOperacao.categoria == "DISPOSITIVO")
        .order_by(HistoricoOperacao.created_at.desc())
        .limit(30)
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
                "command_id": item.command_id,
                "situacao": "Extornado" if item.refunded_at else "Venda Aprovada",
                "refunded_at": item.refunded_at,
                "can_refund": should_allow_refund(
                    pulse_status,
                    item.refunded_at,
                    provider_payment_id,
                    item.provider,
                ),
                "descricao": item.descricao,
            }
        )
    if _should_include_physical_sales(registro_filter, origem_filter, forma_filter, pulso_filter, busca_filter):
        for transacao in pagamentos:
            metodo = transacao.metodo.value if hasattr(transacao.metodo, "value") else str(transacao.metodo)
            if str(metodo).upper() != "FISICO":
                continue
            vendas.append(
                {
                    "id": transacao.id,
                    "kind": "pagamento_fisico",
                    "is_test": False,
                    "data": transacao.data_hora,
                    "valor": float(transacao.valor or 0),
                    "taxa": None,
                    "total": float(transacao.valor or 0),
                    "ponto": maquina.nome_local,
                    "provider": "fisico",
                    "payment_type": metodo,
                    "card_brand": None,
                    "bank_name": None,
                    "provider_payment_id": None,
                    "pulse_status": "fisico",
                    "command_id": None,
                    "situacao": "Pagamento fisico",
                    "refunded_at": None,
                    "can_refund": False,
                    "descricao": "Pagamento fisico registrado pela maquina",
                }
            )
    for item in testes_vendas:
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
                "pulse_status": item.pulse_status or "teste",
                "command_id": item.command_id,
                "situacao": "TESTE",
                "refunded_at": None,
                "can_refund": False,
                "descricao": item.descricao,
            }
        )
    vendas.sort(key=lambda item: item["data"], reverse=True)

    status_online = bool(maquina.ultimo_sinal and (datetime.utcnow() - maquina.ultimo_sinal) < ONLINE_SIGNAL_WINDOW)
    terminal_status = get_active_terminal_for_machine(
        getattr(maquina, "dono", None),
        maquina,
    )
    terminal_payment = recent_terminal_payment_status(db, machine_id)
    if terminal_payment["online"]:
        terminal_status = {
            **terminal_status,
            "status": "online",
            "online": True,
            "terminal_id": terminal_payment["terminal_id"] or terminal_status["terminal_id"],
        }
    ultima_atividade = max(
        [
            item
            for item in [
                ultimo_pagamento.data_hora if ultimo_pagamento else None,
                ultimo_teste.created_at if ultimo_teste else None,
                ultima_saida.data_hora if ultima_saida else None,
            ]
            if item is not None
        ],
        default=None,
    )

    return {
        "range": {"inicio": start_dt, "fim": end_dt},
        "maquina": {
            "id_hardware": maquina.id_hardware,
            "nome": maquina.nome_local,
            "localizacao": maquina.localizacao,
            "banco_pagamento": maquina.banco_pagamento or "mercado_pago",
            "mp_pos_id": maquina.mp_pos_id,
            "mp_pos_external_id": maquina.mp_pos_external_id,
            "terminal_id": terminal_status["terminal_id"],
            "terminal_online": terminal_status["online"],
            "terminal_status": terminal_status["status"],
            "terminal_last_payment_at": terminal_payment["last_payment_at"],
            "firmware_version": maquina.firmware_version,
            "firmware_target_version": maquina.firmware_target_version,
            "firmware_updated_at": maquina.firmware_updated_at,
            "firmware_update_status": maquina.firmware_update_status,
            "firmware_update_command_id": maquina.firmware_update_command_id,
            "firmware_update_url": maquina.firmware_update_url,
            "firmware_update_requested_at": maquina.firmware_update_requested_at,
            "firmware_update_started_at": maquina.firmware_update_started_at,
            "firmware_update_finished_at": maquina.firmware_update_finished_at,
            "cliente_nome": maquina.dono.nome_empresa if getattr(maquina, "dono", None) else None,
            "status_online": status_online,
            "ultimo_sinal": maquina.ultimo_sinal,
            "status_operacional": status_operacional(status_online, ultima_atividade),
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
            for dia, total in sorted(totais_por_dia.items(), key=lambda item: datetime.strptime(item[0], "%d/%m/%Y"))
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
                "pulse_status": teste.pulse_status,
                "command_id": teste.command_id,
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
        "eventos_dispositivo": [
            {
                "id": item.id,
                "maquina_id": item.maquina_id,
                "descricao": item.descricao,
                "pulse_status": item.pulse_status,
                "command_id": item.command_id,
                "created_at": item.created_at,
            }
            for item in eventos_dispositivo
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

from datetime import date, datetime, timedelta
import re

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.models import AuditoriaOperacao, FechamentoMaquina, HistoricoOperacao, Maquina, Transacao, VendaPagamento
from app.services.mercado_pago import get_active_terminal_for_machine
from app.services.pagamentos_helpers import should_allow_refund

OTA_TIMEOUT = timedelta(minutes=3)
OTA_ACTIVE_STATUSES = {"sent", "downloading", "restarting"}


def apply_transacao_periodo(
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
        Transacao.tipo == "IN",
        Transacao.metodo == "FISICO",
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
    if registro == "testes" or origem == "digital" or forma != "todos" or pulso == "ausentes":
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
    quantidade_saidas = (
        db.query(func.count(Transacao.id))
        .filter(
            Transacao.maquina_id == maquina.id_hardware,
            Transacao.tipo == "OUT",
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

    pagamentos = transacoes_query.filter(Transacao.tipo == "IN").order_by(Transacao.data_hora.desc()).all()
    saidas = transacoes_query.filter(Transacao.tipo == "OUT").order_by(Transacao.data_hora.desc()).all()

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

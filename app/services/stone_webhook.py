from datetime import datetime
from uuid import uuid4

from sqlalchemy import text

from app.db.session import SessionLocal
from app.models.models import (
    Cliente,
    EventoTipo,
    HistoricoOperacao,
    Maquina,
    MetodoPagamento,
    Transacao,
    VendaPagamento,
)
from app.services.command_queue import get_command_status
from app.services.mqtt_commands import publish_machine_credit_pulses
from app.services.pagamentos_helpers import calcular_pulsos_por_valor
from app.services.pulse_tracking import update_pulse_status
from app.services.stone import (
    StoneAPIError,
    charge_amount,
    charge_card_brand,
    charge_payment_type,
    extract_terminal_serial,
    get_charge,
    normalize_serial,
    stone_credentials,
)
from app.services.vendas import registrar_venda_pagamento

PAID_STATUSES = {"paid"}
REFUNDED_STATUSES = {"refunded", "canceled", "chargedback"}
# 401/403/404 no GET da cobranca = a chave testada e' de outra conta Stone
# (outro cliente) - tenta a proxima em vez de abortar o webhook.
OUTRA_CONTA_STATUSES = {401, 403, 404}


def _acquire_payment_lock(db, key: str) -> None:
    """Mesma trava usada no webhook do Mercado Pago/SumUp - serializa o
    webhook e o poller processando a mesma cobranca ao mesmo tempo."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})


def find_maquina_by_serial(db, serial: str, cliente_id: int | None = None) -> Maquina | None:
    serial = normalize_serial(serial)
    if not serial:
        return None
    query = db.query(Maquina).filter(Maquina.banco_pagamento == "stone", Maquina.stone_serial_number == serial)
    if cliente_id is not None:
        query = query.filter(Maquina.cliente_id == cliente_id)
    maquinas = query.all()
    # Serial repetido em duas maquinas: nao da' pra saber qual creditar.
    return maquinas[0] if len(maquinas) == 1 else None


def registrar_pagamento_stone(
    db,
    *,
    machine_id: str,
    amount: float,
    provider_payment_id: str,
    descricao: str,
    payment_type: str | None = None,
    card_brand: str | None = None,
) -> str | None:
    """Grava Transacao/HistoricoOperacao/VendaPagamento e devolve o command_id
    pra liberar o pulso - ou None se a cobranca ja foi processada (o webhook e
    o poller podem ver a mesma cobranca)."""
    _acquire_payment_lock(db, f"stone_payment_{provider_payment_id}")
    duplicado = (
        db.query(VendaPagamento)
        .filter(
            VendaPagamento.provider == "stone",
            VendaPagamento.provider_payment_id == provider_payment_id,
        )
        .first()
    )
    if duplicado:
        return None

    command_id = str(uuid4())
    transacao = Transacao(
        maquina_id=machine_id,
        tipo=EventoTipo.in_flux,
        metodo=MetodoPagamento.digital,
        valor=amount,
        data_hora=datetime.utcnow(),
    )
    db.add(transacao)
    historico = HistoricoOperacao(
        maquina_id=machine_id,
        categoria="PAGAMENTO",
        descricao=descricao,
        valor=amount,
        provider="stone",
        provider_payment_id=provider_payment_id,
        payment_type=payment_type,
        card_brand=card_brand,
        pulse_status="pendente",
        command_id=command_id,
        created_at=transacao.data_hora,
    )
    db.add(historico)
    db.flush()
    registrar_venda_pagamento(
        db,
        maquina_id=machine_id,
        valor=amount,
        origem="stone",
        transacao_id=transacao.id,
        historico_id=historico.id,
        provider="stone",
        provider_payment_id=provider_payment_id,
        tipo_pagamento=payment_type,
        bandeira_cartao=card_brand,
        status_pulso="pendente",
        command_id=command_id,
        created_at=transacao.data_hora,
    )
    db.commit()
    return command_id


def liberar_pulso_stone(machine_id: str, amount: float, command_id: str) -> str:
    pulsos = calcular_pulsos_por_valor(amount)
    try:
        publish_machine_credit_pulses(machine_id, pulses=pulsos, action="paid", command_id=command_id, amount=amount)
    except Exception:
        update_pulse_status(command_id, "falha_publicacao")
        raise
    return get_command_status(command_id) or "pendente"


def marcar_estorno_stone(db, charge_id: str) -> bool:
    """O estorno e' feito na maquininha/Portal Stone (nao ha API) - quando a
    Stone avisa (charge.refunded), so' marcamos no historico e na venda."""
    vendas = (
        db.query(VendaPagamento)
        .filter(VendaPagamento.provider == "stone", VendaPagamento.provider_payment_id == charge_id)
        .all()
    )
    historicos = (
        db.query(HistoricoOperacao)
        .filter(HistoricoOperacao.provider == "stone", HistoricoOperacao.provider_payment_id == charge_id)
        .all()
    )
    agora = datetime.utcnow()
    alterado = False
    for registro in [*vendas, *historicos]:
        if not registro.refunded_at:
            registro.refunded_at = agora
            alterado = True
    if alterado:
        db.commit()
    return alterado


def processar_charge_paga(db, maquina: Maquina, charge: dict, origem: str) -> str | None:
    """Registra uma cobranca Stone ja confirmada pela API. Devolve o
    command_id (o pulso e' liberado por quem chamou, fora da sessao) ou None
    se ja tinha sido processada."""
    charge_id = str(charge.get("id") or "").strip()
    serial = extract_terminal_serial(charge) or maquina.stone_serial_number
    return registrar_pagamento_stone(
        db,
        machine_id=maquina.id_hardware,
        amount=charge_amount(charge),
        provider_payment_id=charge_id,
        descricao=f"Pagamento aprovado {origem} maquininha Stone (charge_id={charge_id}, terminal_id={serial})",
        payment_type=charge_payment_type(charge),
        card_brand=charge_card_brand(charge),
    )


def _buscar_charge_confirmada(db, charge_id: str, serial_hint: str) -> tuple[dict | None, Cliente | None]:
    """Nunca confia no corpo do webhook (a rota e' publica): busca a cobranca
    na API da Stone com a chave do cliente. Se o serial do webhook aponta pra
    uma maquina nossa, tenta primeiro a chave do dono dela; senao testa as
    chaves de todos os clientes Stone."""
    maquina_hint = find_maquina_by_serial(db, serial_hint)
    dono_hint = maquina_hint.dono if maquina_hint else None
    candidatos: list[Cliente | None] = [dono_hint] if dono_hint else []
    candidatos += [
        cliente
        for cliente in db.query(Cliente).filter(Cliente.stone_secret_key.isnot(None)).all()
        if not dono_hint or cliente.id != dono_hint.id
    ]
    if not candidatos:
        # Sem chave em nenhum cliente: ainda tenta o fallback STONE_SECRET_KEY do .env.
        candidatos.append(None)

    for cliente in candidatos:
        secret_key, referer = stone_credentials(cliente)
        if not secret_key:
            continue
        try:
            return get_charge(secret_key, charge_id, referer), cliente
        except StoneAPIError as exc:
            if exc.provider_status in OUTRA_CONTA_STATUSES:
                continue
            raise
    return None, None


def processar_callback_stone(dados: dict):
    print(f"[Stone webhook] payload={dados}")
    evento = str(dados.get("type") or "").strip().lower()
    data = dados.get("data") or {}
    charge_id = str(data.get("id") or "").strip()
    if not charge_id.startswith("ch_"):
        return {"status": "ignorado", "detalhe": f"Evento sem cobranca ({evento or 'sem type'})"}

    db = SessionLocal()
    try:
        charge, cliente = _buscar_charge_confirmada(db, charge_id, extract_terminal_serial(data))
        if not charge:
            print(f"[Stone webhook] ignorado: cobranca {charge_id} nao encontrada em nenhuma conta Stone cadastrada")
            return {"status": "ignorado", "detalhe": "Cobranca nao encontrada nas contas Stone cadastradas"}

        status_raw = str(charge.get("status") or "").strip().lower()
        if evento == "charge.refunded" or status_raw in REFUNDED_STATUSES:
            marcado = marcar_estorno_stone(db, charge_id)
            print(f"[Stone webhook] estorno charge_id={charge_id} marcado={marcado}")
            return {"status": "sucesso" if marcado else "ignorado", "detalhe": "Estorno registrado" if marcado else "Nada a estornar"}

        if status_raw not in PAID_STATUSES:
            return {"status": "ignorado", "detalhe": f"Cobranca nao paga ({status_raw})"}

        serial = extract_terminal_serial(charge) or extract_terminal_serial(data)
        maquina = find_maquina_by_serial(db, serial, cliente_id=getattr(cliente, "id", None))
        if not maquina:
            # Maquininha da mesma conta Stone que nao esta ligada a nenhuma
            # maquina do CompactPay (ex.: usada no caixa do estabelecimento).
            print(f"[Stone webhook] ignorado: serial {serial or '(vazio)'} sem maquina Stone cadastrada (charge_id={charge_id})")
            return {"status": "ignorado", "detalhe": "Maquininha sem maquina cadastrada no CompactPay"}

        machine_id = maquina.id_hardware
        amount = charge_amount(charge)
        command_id = processar_charge_paga(db, maquina, charge, "direto na")
        if command_id is None:
            return {"status": "ignorado", "detalhe": "Pagamento ja processado"}
    finally:
        db.close()

    command_status = liberar_pulso_stone(machine_id, amount, command_id)
    print(f"[Stone webhook] processado charge_id={charge_id} serial={serial} machine={machine_id} amount={amount} command_status={command_status}")
    return {
        "status": "sucesso",
        "detalhe": "Pagamento aprovado, pulsos enviados",
        "machine_id": machine_id,
        "terminal_id": serial,
        "pulsos": calcular_pulsos_por_valor(amount),
        "command_status": command_status,
    }

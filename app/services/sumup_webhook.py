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
    SumupCheckout,
    Transacao,
    VendaPagamento,
)
from app.services.command_queue import get_command_status
from app.services.mqtt_commands import publish_machine_credit_pulses
from app.services.pagamentos_helpers import calcular_pulsos_por_valor
from app.services.pulse_tracking import update_pulse_status
from app.services.sumup import get_reader_checkout
from app.services.vendas import registrar_venda_pagamento

# Status finais que a SumUp pode mandar no GET do checkout - qualquer outra
# coisa (ex.: "pending") e tratada como "ainda nao decidiu, ignora por agora".
SUCCESS_STATUSES = {"paid", "successful"}
FAILED_STATUSES = {"failed", "expired", "cancelled", "canceled"}


def _acquire_payment_lock(db, key: str) -> None:
    """Mesma trava usada no webhook do Mercado Pago - serializa notificacoes
    quase simultaneas do mesmo checkout."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})


def registrar_pagamento_sumup(
    db,
    *,
    machine_id: str,
    amount: float,
    provider_payment_id: str,
    descricao: str,
    payment_type: str | None = None,
) -> str | None:
    """Grava Transacao/HistoricoOperacao/VendaPagamento pro pagamento SumUp e
    devolve o command_id pra liberar o pulso - ou None se ja foi processado
    antes (duplicidade). Usado tanto pelo webhook (checkout criado por nos)
    quanto pelo poller (pagamento feito direto na maquininha, standalone)."""
    _acquire_payment_lock(db, f"sumup_payment_{provider_payment_id}")
    duplicado = (
        db.query(VendaPagamento)
        .filter(
            VendaPagamento.provider == "sumup",
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
        provider="sumup",
        provider_payment_id=provider_payment_id,
        payment_type=payment_type,
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
        origem="sumup",
        transacao_id=transacao.id,
        historico_id=historico.id,
        provider="sumup",
        provider_payment_id=provider_payment_id,
        tipo_pagamento=payment_type,
        status_pulso="pendente",
        command_id=command_id,
        created_at=transacao.data_hora,
    )
    db.commit()
    return command_id


def liberar_pulso_sumup(machine_id: str, amount: float, command_id: str) -> str:
    pulsos = calcular_pulsos_por_valor(amount)
    try:
        publish_machine_credit_pulses(machine_id, pulses=pulsos, action="paid", command_id=command_id, amount=amount)
    except Exception:
        update_pulse_status(command_id, "falha_publicacao")
        raise
    return get_command_status(command_id) or "pendente"


def processar_callback_sumup(dados: dict):
    print(f"[SumUp webhook] payload={dados}")

    # O webhook da SumUp so manda {event_type, id} (id do checkout) - sem
    # machine_id, reader_id ou valor. Por isso guardamos esses dados nos
    # em sumup_checkouts no momento em que a cobranca e' criada (ver
    # cobrar_na_maquininha_sumup em pagamentos.py) e consultamos aqui.
    checkout_id = str(dados.get("id") or ((dados.get("payload") or {}).get("client_transaction_id")) or "").strip()
    if not checkout_id:
        print("[SumUp webhook] ignorado: sem id de checkout")
        return {"status": "ignorado", "detalhe": "Webhook sem id de checkout"}

    db = SessionLocal()
    try:
        pendente = db.query(SumupCheckout).filter(SumupCheckout.checkout_id == checkout_id).first()
        if not pendente:
            print(f"[SumUp webhook] ignorado: checkout_id nao encontrado localmente ({checkout_id})")
            return {"status": "ignorado", "detalhe": "Checkout desconhecido (nao foi criado por este sistema)"}
        if pendente.processado:
            print(f"[SumUp webhook] ja processado checkout_id={checkout_id}")
            return {"status": "ignorado", "detalhe": "Pagamento ja processado"}

        machine_id = pendente.maquina_id
        reader_id = pendente.reader_id
        amount = pendente.valor

        maquina = db.query(Maquina).filter(Maquina.id_hardware == machine_id).first()
        cliente = db.query(Cliente).filter(Cliente.id == maquina.cliente_id).first() if maquina and maquina.cliente_id else None
        access_token = ((getattr(cliente, "sumup_api_key", None) or "") if cliente else "").strip()
        merchant_code = ((getattr(cliente, "sumup_merchant_code", None) or "") if cliente else "").strip()
        if not access_token or not merchant_code:
            print(f"[SumUp webhook] ignorado: cliente da maquina {machine_id} sem credenciais SumUp")
            return {"status": "erro", "detalhe": "Cliente da maquina sem credenciais SumUp cadastradas"}

        checkout_data = get_reader_checkout(access_token, merchant_code, reader_id, checkout_id)
        status_raw = str(checkout_data.get("status") or "").strip().lower()

        if status_raw not in SUCCESS_STATUSES:
            if status_raw in FAILED_STATUSES:
                pendente.processado = True
                db.commit()
                print(f"[SumUp webhook] checkout {checkout_id} finalizado sem sucesso (status={status_raw})")
                return {"status": "ignorado", "detalhe": f"Pagamento nao aprovado ({status_raw})"}
            print(f"[SumUp webhook] checkout {checkout_id} ainda pendente (status={status_raw})")
            return {"status": "ignorado", "detalhe": f"Checkout ainda nao finalizado ({status_raw})"}

        # O estorno da SumUp usa o transaction_id, nao o checkout_id (ver
        # create_sumup_refund) - guardamos ele como provider_payment_id pra
        # deixar o extorno manual/automatico (pagamentos_helpers.py) funcionar
        # sem precisar consultar a SumUp de novo depois.
        transaction_id = str(checkout_data.get("transaction_id") or checkout_id)

        command_id = registrar_pagamento_sumup(
            db,
            machine_id=machine_id,
            amount=amount,
            provider_payment_id=transaction_id,
            descricao=f"Pagamento aprovado via maquininha SumUp (checkout_id={checkout_id}, reader_id={reader_id})",
            payment_type=checkout_data.get("card_type") or checkout_data.get("payment_type"),
        )
        pendente.processado = True
        db.commit()
        if command_id is None:
            print(f"[SumUp webhook] pagamento duplicado checkout_id={checkout_id}")
            return {"status": "ignorado", "detalhe": "Pagamento ja processado"}
    finally:
        db.close()

    command_status = liberar_pulso_sumup(machine_id, amount, command_id)
    print(
        f"[SumUp webhook] checkout processado checkout_id={checkout_id} reader={reader_id} "
        f"machine={machine_id} amount={amount} pulsos={pulsos} command_status={command_status}"
    )
    return {
        "status": "sucesso",
        "detalhe": "Pagamento aprovado, pulsos enviados",
        "machine_id": machine_id,
        "terminal_id": reader_id,
        "pulsos": pulsos,
        "command_status": command_status,
    }

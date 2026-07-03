from datetime import datetime
from uuid import uuid4

from app.db.session import SessionLocal
from app.models.models import EscutaTerminal, EventoTipo, HistoricoOperacao, Maquina, MetodoPagamento, Transacao
from app.services.mqtt_commands import publish_machine_credit_pulses
from app.services.pagamentos_helpers import (
    calcular_pulsos_por_valor,
    extract_mp_location_ids,
    extract_terminal_id,
    mp_request_with_known_tokens,
    parse_machine_id_from_external_reference,
    payment_metadata,
)
from app.services.pulse_tracking import update_pulse_status, wait_for_pulse_confirmation
from app.services.vendas import registrar_venda_pagamento

PROCESSED_PAYMENT_IDS: set[str] = set()


def _matches_any(saved_value: str | None, received_values: set[str]) -> bool:
    value = str(saved_value or "").strip()
    return bool(value and value in received_values)


def _resolve_machine_by_mp_location(db, payment_data: dict):
    ids = extract_mp_location_ids(payment_data)
    has_pos = bool(ids["pos_ids"] or ids["pos_external_ids"])
    has_store = bool(ids["store_ids"] or ids["store_external_ids"])
    if not has_pos and not has_store:
        return None, ids, "sem_caixa_loja"

    candidates = []
    for maquina in db.query(Maquina).all():
        if (maquina.banco_pagamento or "mercado_pago") != "mercado_pago":
            continue
        pos_match = _matches_any(maquina.mp_pos_id, ids["pos_ids"]) or _matches_any(
            maquina.mp_pos_external_id,
            ids["pos_external_ids"],
        )
        store_match = _matches_any(maquina.mp_store_id, ids["store_ids"]) or _matches_any(
            maquina.mp_store_external_id,
            ids["store_external_ids"],
        )
        if has_pos and has_store:
            matched = pos_match and store_match
        elif has_pos:
            matched = pos_match
        else:
            matched = store_match
        if matched:
            candidates.append(maquina)

    if len(candidates) == 1:
        return candidates[0], ids, "match_unico"
    if len(candidates) > 1:
        return None, ids, "match_multiplo"
    return None, ids, "sem_match"


def processar_callback_mercado_pago(dados: dict):
    print(f"[MP webhook] payload={dados}")

    # Suporta payload simples antigo: {status, id_hardware, valor}
    if dados.get("status") == "approved" and dados.get("id_hardware"):
        id_hardware = dados.get("id_hardware")
        valor = float(dados.get("valor", 1.0))
        db = SessionLocal()
        command_id = str(uuid4())
        try:
            nova_transacao = Transacao(
                maquina_id=id_hardware,
                tipo=EventoTipo.in_flux,
                metodo=MetodoPagamento.digital,
                valor=valor,
                data_hora=datetime.utcnow(),
            )
            db.add(nova_transacao)
            historico = HistoricoOperacao(
                maquina_id=id_hardware,
                categoria="PAGAMENTO",
                descricao="Pagamento aprovado via callback simplificado",
                valor=valor,
                provider="mercado_pago",
                pulse_status="pendente",
                command_id=command_id,
                created_at=nova_transacao.data_hora,
            )
            db.add(historico)
            db.flush()
            registrar_venda_pagamento(
                db,
                maquina_id=id_hardware,
                valor=valor,
                origem="mercado_pago",
                transacao_id=nova_transacao.id,
                historico_id=historico.id,
                provider="mercado_pago",
                status_pulso="pendente",
                command_id=command_id,
                created_at=nova_transacao.data_hora,
            )
            db.commit()
        finally:
            db.close()

        pulsos = calcular_pulsos_por_valor(valor)
        publish_machine_credit_pulses(id_hardware, pulses=pulsos, action="paid", command_id=command_id, amount=valor)
        pulse_status = wait_for_pulse_confirmation(
            command_id,
            timeout_seconds=max(8, pulsos * 2),
        )
        print(f"[MP webhook] callback simplificado aprovado maquina={id_hardware} valor={valor} pulsos={pulsos}")
        return {"status": "sucesso", "detalhe": "Pagamento digital registrado", "pulsos": pulsos, "pulse_status": pulse_status}

    topic = dados.get("topic") or dados.get("type") or ""
    action = dados.get("action") or ""
    data = dados.get("data") or {}
    order_id = data.get("id") or dados.get("id") or dados.get("data.id")

    if not order_id:
        print("[MP webhook] ignorado: sem id de order/payment")
        return {"status": "ignorado", "detalhe": "Webhook sem id de order/payment"}

    # Para webhook da nova API /v1/orders: type=order action=order.processed
    if topic == "order" or action.startswith("order.") or str(order_id).startswith("ORD"):
        db_lookup = SessionLocal()
        try:
            order_data, _ = mp_request_with_known_tokens(
                db_lookup,
                "GET",
                f"https://api.mercadopago.com/v1/orders/{order_id}",
            )
        finally:
            db_lookup.close()
        order_status = (order_data.get("status") or "").lower()
        if order_status not in {"processed"} and action != "order.processed":
            print(f"[MP webhook] order ignorada: status={order_status} action={action}")
            return {"status": "ignorado", "detalhe": f"Order ainda nao aprovada ({order_status or action})"}

        external_reference = order_data.get("external_reference")
        machine_id = parse_machine_id_from_external_reference(external_reference)
        if not machine_id:
            print("[MP webhook] order ignorada: sem machine_id no external_reference")
            return {"status": "erro", "detalhe": "Nao foi possivel identificar machine_id no external_reference"}

        amount = 1.0
        payments = ((order_data.get("transactions") or {}).get("payments") or [])
        if payments:
            amount = float(payments[0].get("amount") or 1.0)

        db = SessionLocal()
        command_id = str(uuid4())
        try:
            duplicado = (
                db.query(HistoricoOperacao)
                .filter(
                    HistoricoOperacao.maquina_id == machine_id,
                    HistoricoOperacao.categoria == "PAGAMENTO",
                    HistoricoOperacao.descricao.contains(f"mp_order_id={order_id}"),
                )
                .first()
            )
            if duplicado:
                print(f"[MP webhook] order duplicada mp_order_id={order_id}")
                return {"status": "ignorado", "detalhe": "Pagamento ja processado"}

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
                descricao=f"Pagamento aprovado via maquininha MP (mp_order_id={order_id})",
                valor=amount,
                provider="mercado_pago",
                provider_payment_id=str(order_id),
                payment_type="order",
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
                origem="mercado_pago",
                transacao_id=transacao.id,
                historico_id=historico.id,
                provider="mercado_pago",
                provider_payment_id=str(order_id),
                tipo_pagamento="order",
                status_pulso="pendente",
                command_id=command_id,
                created_at=transacao.data_hora,
            )
            db.commit()
            historico_id = historico.id
        finally:
            db.close()

        pulsos = calcular_pulsos_por_valor(amount)
        publish_machine_credit_pulses(machine_id, pulses=pulsos, action="paid", command_id=command_id, amount=amount)
        pulse_status = wait_for_pulse_confirmation(
            command_id,
            timeout_seconds=max(8, pulsos * 2),
        )
        print(f"[MP webhook] order processada machine={machine_id} amount={amount} pulsos={pulsos}")
        return {"status": "sucesso", "detalhe": "Pagamento aprovado e pulsos enviados", "pulsos": pulsos, "pulse_status": pulse_status}

    is_payment_event = topic in {"payment"} or action.startswith("payment.")
    if is_payment_event:
        payment_id = str((dados.get("data") or {}).get("id") or dados.get("id") or dados.get("data.id") or "").strip()
        if not payment_id:
            print("[MP webhook] payment ignorado: sem payment_id")
            return {"status": "ignorado", "detalhe": "Evento payment sem id"}
        db_lookup = SessionLocal()
        try:
            payment_data, _ = mp_request_with_known_tokens(
                db_lookup,
                "GET",
                f"https://api.mercadopago.com/v1/payments/{payment_id}",
            )
        finally:
            db_lookup.close()
        payment_status = (payment_data.get("status") or "").lower()
        if payment_status not in {"approved", "authorized"}:
            print(f"[MP webhook] payment ignorado: payment_id={payment_id} status={payment_status}")
            return {"status": "ignorado", "detalhe": f"Pagamento ainda nao aprovado ({payment_status})"}

        terminal_id = extract_terminal_id(payment_data)
        amount = float(payment_data.get("transaction_amount") or 1.0)

        db = SessionLocal()
        command_id = str(uuid4())
        try:
            duplicado = (
                db.query(HistoricoOperacao)
                .filter(
                    HistoricoOperacao.categoria == "PAGAMENTO",
                    HistoricoOperacao.descricao.contains(f"payment_id={payment_id}"),
                )
                .first()
            )
            if duplicado:
                print(f"[MP webhook] payment duplicado payment_id={payment_id}")
                return {"status": "ignorado", "detalhe": "Pagamento ja processado"}

            escuta = None
            if terminal_id:
                escuta = (
                    db.query(EscutaTerminal)
                    .filter(EscutaTerminal.terminal_id == terminal_id, EscutaTerminal.ativo.is_(True))
                    .first()
                )
            machine_id = escuta.maquina_id if escuta else None
            if not escuta:
                maquina_por_caixa, mp_location_ids, match_reason = _resolve_machine_by_mp_location(db, payment_data)
                if maquina_por_caixa:
                    machine_id = maquina_por_caixa.id_hardware
                    print(
                        f"[MP webhook] payment vinculado por caixa/loja payment_id={payment_id} maquina={machine_id} ids={mp_location_ids}"
                    )
                elif terminal_id:
                    print(
                        f"[MP webhook] payment ignorado: terminal sem escuta ativa e sem match de caixa/loja terminal_id={terminal_id} payment_id={payment_id} motivo={match_reason} ids={mp_location_ids}"
                    )
                    detalhe = "Terminal sem escuta ativa e sem match seguro de caixa/loja"
                else:
                    print(
                        f"[MP webhook] payment ignorado: sem terminal_id e sem match seguro de caixa/loja payment_id={payment_id} motivo={match_reason} ids={mp_location_ids}"
                    )
                    detalhe = "Mercado Pago nao retornou terminal_id nem caixa/loja compativel com uma unica maquina"
                if not machine_id:
                    return {
                        "status": "ignorado",
                        "detalhe": detalhe,
                        "terminal_id": terminal_id,
                        "payment_id": payment_id,
                        "mp_location_ids": {key: sorted(value) for key, value in mp_location_ids.items()},
                    }

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
                descricao=f"Pagamento maquininha aprovado (payment_id={payment_id}, terminal_id={terminal_id or 'n/a'})",
                valor=amount,
                created_at=transacao.data_hora,
                **payment_metadata(payment_data),
                command_id=command_id,
            )
            db.add(historico)
            db.flush()
            registrar_venda_pagamento(
                db,
                maquina_id=machine_id,
                valor=amount,
                origem="mercado_pago",
                transacao_id=transacao.id,
                historico_id=historico.id,
                provider="mercado_pago",
                provider_payment_id=payment_id,
                tipo_pagamento=historico.payment_type,
                bandeira_cartao=historico.card_brand,
                banco=historico.bank_name,
                status_pulso="pendente",
                command_id=command_id,
                created_at=transacao.data_hora,
            )
            db.commit()
            db.refresh(historico)
            historico_id = historico.id
        finally:
            db.close()

        pulsos = calcular_pulsos_por_valor(amount)
        try:
            publish_machine_credit_pulses(machine_id, pulses=pulsos, action="paid", command_id=command_id, amount=amount)
            pulse_status = wait_for_pulse_confirmation(
                command_id,
                timeout_seconds=max(8, pulsos * 2),
            )
        except Exception:
            update_pulse_status(command_id, "falha_publicacao")
            raise
        PROCESSED_PAYMENT_IDS.add(payment_id)
        print(
            f"[MP webhook] payment processado payment_id={payment_id} terminal={terminal_id} machine={machine_id} amount={amount} pulsos={pulsos}"
        )
        return {
            "status": "sucesso",
            "detalhe": "Pagamento recebido e pulsos enviados",
            "machine_id": machine_id,
            "terminal_id": terminal_id,
            "pulsos": pulsos,
            "pulse_status": pulse_status,
        }

    print(f"[MP webhook] ignorado: evento nao tratado topic={topic} action={action}")
    return {"status": "ignorado", "detalhe": "Evento nao tratado neste endpoint"}

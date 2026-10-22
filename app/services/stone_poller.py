import logging
import threading
import time
from datetime import datetime, timedelta

from fastapi import HTTPException

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.models import Cliente, Maquina, VendaPagamento
from app.services.stone import charge_amount, extract_terminal_serial, list_paid_charges, stone_credentials
from app.services.stone_webhook import liberar_pulso_stone, processar_charge_paga

# Rede de seguranca do webhook charge.paid: se a Stone nao conseguir entregar
# (backend fora do ar, deploy), a venda feita na maquininha ainda e' achada
# aqui. Reler uma janela maior nao credita em dobro - registrar_pagamento_stone
# ignora cobranca ja gravada.
CREATED_SINCE_SAFETY_MARGIN = timedelta(minutes=5)
# Primeira rodada de um cliente novo: nao reprocessa o historico inteiro da
# conta Stone, so' o que for recente.
FIRST_SYNC_LOOKBACK = timedelta(hours=1)


def _ja_processada(db, charge_id: str) -> bool:
    return bool(
        db.query(VendaPagamento.id)
        .filter(VendaPagamento.provider == "stone", VendaPagamento.provider_payment_id == charge_id)
        .first()
    )


def process_cliente(db, cliente: Cliente) -> None:
    secret_key, referer = stone_credentials(cliente)
    if not secret_key:
        return

    maquinas = (
        db.query(Maquina)
        .filter(Maquina.cliente_id == cliente.id, Maquina.banco_pagamento == "stone", Maquina.stone_serial_number.isnot(None))
        .all()
    )
    maquina_by_serial: dict[str, Maquina] = {}
    repetidos: set[str] = set()
    for maquina in maquinas:
        if maquina.stone_serial_number in maquina_by_serial:
            repetidos.add(maquina.stone_serial_number)
        maquina_by_serial[maquina.stone_serial_number] = maquina
    for serial in repetidos:
        maquina_by_serial.pop(serial, None)
    if not maquina_by_serial:
        return

    inicio = datetime.utcnow()
    since = (cliente.stone_last_sync_at - CREATED_SINCE_SAFETY_MARGIN) if cliente.stone_last_sync_at else (inicio - FIRST_SYNC_LOOKBACK)
    try:
        charges = list_paid_charges(secret_key, since.strftime("%Y-%m-%dT%H:%M:%SZ"), referer)
    except HTTPException as exc:
        logging.warning("[Stone poller] falha ao consultar cobrancas do cliente %s: %s", cliente.id, exc.detail)
        return

    for charge in charges:
        charge_id = str(charge.get("id") or "").strip()
        if not charge_id or _ja_processada(db, charge_id):
            continue
        maquina = maquina_by_serial.get(extract_terminal_serial(charge))
        if not maquina:
            continue
        command_id = processar_charge_paga(db, maquina, charge, "(poller) direto na")
        if command_id is None:
            continue
        amount = charge_amount(charge)
        command_status = liberar_pulso_stone(maquina.id_hardware, amount, command_id)
        logging.warning(
            "[Stone poller] cobranca %s achada sem webhook - machine=%s amount=%s command_status=%s",
            charge_id, maquina.id_hardware, amount, command_status,
        )

    cliente.stone_last_sync_at = inicio
    db.commit()


def poll_all_clientes() -> None:
    db = SessionLocal()
    try:
        cliente_ids = [
            cliente_id
            for (cliente_id,) in db.query(Maquina.cliente_id)
            .filter(Maquina.banco_pagamento == "stone", Maquina.stone_serial_number.isnot(None), Maquina.cliente_id.isnot(None))
            .distinct()
            .all()
        ]
        for cliente_id in cliente_ids:
            cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
            if not cliente:
                continue
            try:
                process_cliente(db, cliente)
            except Exception:
                db.rollback()
                logging.exception("[Stone poller] erro processando cliente %s", cliente_id)
    finally:
        db.close()


def run_stone_poller_worker() -> None:
    logging.info("Stone poller worker iniciado (intervalo=%ss)", settings.STONE_POLLER_INTERVAL_SECONDS)
    while True:
        try:
            poll_all_clientes()
        except Exception:
            logging.exception("[Stone poller] erro no worker")
        time.sleep(settings.STONE_POLLER_INTERVAL_SECONDS)


def start_stone_poller_worker() -> threading.Thread | None:
    if not settings.START_STONE_POLLER_WORKER:
        logging.info("Stone poller worker desativado por START_STONE_POLLER_WORKER=false")
        return None
    thread = threading.Thread(target=run_stone_poller_worker, daemon=True)
    thread.start()
    return thread

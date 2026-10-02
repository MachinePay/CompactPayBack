import json
import logging
import threading
import time
from datetime import datetime, timedelta

from fastapi import HTTPException

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.models import Cliente, Maquina, SumupTransacaoPendente, VendaPagamento
from app.services.sumup import build_reader_device_map, get_transaction_details, list_recent_transactions
from app.services.sumup_webhook import liberar_pulso_sumup, registrar_pagamento_sumup

# Status que a API de historico da SumUp pode mandar - tratamos qualquer
# variacao de caixa/alias como "foi aprovado", o resto e' ignorado.
SUCCESS_STATUSES = {"successful", "success", "paid"}

# Margem de seguranca subtraida do changes_since - sem isso, uma transacao que
# demore pra ficar visivel no historico da SumUp (relogio dessincronizado,
# atraso de processamento no lado deles) podia nunca ser vista: o watermark
# ja tinha avancado pra depois do timestamp dela antes dela aparecer. Reler
# uma janela maior nao credita nada em dobro - _ja_processada ja filtra
# qualquer transacao que a gente ja viu antes.
CHANGES_SINCE_SAFETY_MARGIN = timedelta(minutes=5)


def _extract_device_identifier(transacao: dict) -> str | None:
    device_info = transacao.get("device_info") or {}
    for key in ("uuid", "identifier", "serial", "serial_number"):
        value = device_info.get(key)
        if value:
            return str(value)
    return None


def _resolve_maquina(
    device_identifier: str | None,
    maquina_by_reader_id: dict[str, Maquina],
    single_machine: Maquina | None,
    reader_device_map: dict[str, str],
) -> tuple[Maquina | None, str]:
    """SEMPRE verifica contra o reader real antes de creditar - nunca credita
    'no escuro' so porque o cliente tem uma maquina so. Prioridade:
    1. device_identifier bate com o serial de um reader vinculado a uma
       maquina conhecida -> credita essa maquina (verificado de verdade).
    2. device_identifier veio mas NAO bate com nenhum reader vinculado -> e'
       sinal de reader desconhecido/nao cadastrado na conta - fica pendente
       mesmo que so' exista uma maquina, pra nao arriscar creditar errado.
    3. A SumUp nao devolveu device_info nessa transacao (falha/campo ausente)
       e o cliente so tem UMA maquina -> credita por falta de alternativa,
       mas loga como fallback (nao verificado) pra ficar rastreavel.
    """
    if device_identifier:
        reader_id = reader_device_map.get(device_identifier)
        if reader_id and reader_id in maquina_by_reader_id:
            return maquina_by_reader_id[reader_id], "verificado_por_device"
        return None, "device_sem_reader_correspondente"
    if single_machine is not None:
        return single_machine, "fallback_sem_device_info"
    return None, "sem_device_info_multiplas_maquinas"


def _ja_processada(db, transaction_id: str) -> bool:
    return bool(
        db.query(VendaPagamento.id)
        .filter(VendaPagamento.provider == "sumup", VendaPagamento.provider_payment_id == transaction_id)
        .first()
    ) or bool(
        db.query(SumupTransacaoPendente.id)
        .filter(SumupTransacaoPendente.transaction_id == transaction_id)
        .first()
    )


def process_cliente(db, cliente: Cliente) -> None:
    access_token = (cliente.sumup_api_key or "").strip()
    merchant_code = (cliente.sumup_merchant_code or "").strip()
    if not access_token or not merchant_code:
        return

    maquinas = (
        db.query(Maquina)
        .filter(Maquina.cliente_id == cliente.id, Maquina.sumup_reader_id.isnot(None))
        .all()
    )
    if not maquinas:
        return

    maquina_by_reader_id = {m.sumup_reader_id: m for m in maquinas if m.sumup_reader_id}
    single_machine = maquinas[0] if len(maquinas) == 1 else None

    # Formato exato da doc oficial ("2019-08-28T09:00:00Z") - sem isso,
    # .isoformat() manda microssegundos e sem "Z" (ex.: "...23:08:39.967797"),
    # formato que a API pode nao reconhecer e silenciosamente nao filtrar
    # nada (sem erro, so' zero resultados sempre).
    changes_since = (
        (cliente.sumup_last_sync_at - CHANGES_SINCE_SAFETY_MARGIN).strftime("%Y-%m-%dT%H:%M:%SZ")
        if cliente.sumup_last_sync_at
        else None
    )
    try:
        transacoes = list_recent_transactions(access_token, merchant_code, changes_since=changes_since)
    except HTTPException as exc:
        logging.warning("[SumUp poller] falha ao consultar historico do cliente %s: %s", cliente.id, exc.detail)
        return
    logging.info(
        "[SumUp poller] cliente %s: %d transacao(oes) retornada(s) desde %s",
        cliente.id, len(transacoes), changes_since,
    )

    # Mapa device_identifier -> reader_id - buscado sempre (nao so' quando ha
    # mais de uma maquina), pra SEMPRE poder confirmar contra o reader real
    # antes de creditar em vez de assumir a maquina pelo numero de maquinas.
    # So guardamos os readers que estao de fato vinculados a uma das nossas
    # maquinas (maquina_by_reader_id) - a conta SumUp do cliente pode ter
    # outros readers que nao tem nada a ver com o nosso sistema.
    try:
        reader_device_map_completo = build_reader_device_map(access_token, merchant_code)
    except HTTPException as exc:
        logging.warning("[SumUp poller] falha ao listar readers do cliente %s: %s", cliente.id, exc.detail)
        reader_device_map_completo = {}
    reader_device_map = {
        identifier: reader_id
        for identifier, reader_id in reader_device_map_completo.items()
        if reader_id in maquina_by_reader_id
    }

    for transacao in transacoes:
        status_raw = str(transacao.get("status") or "").strip().lower()
        if status_raw not in SUCCESS_STATUSES:
            continue

        transaction_id = str(transacao.get("id") or transacao.get("transaction_id") or "").strip()
        if not transaction_id:
            continue
        if _ja_processada(db, transaction_id):
            continue

        try:
            valor = float(transacao.get("amount"))
        except (TypeError, ValueError):
            logging.warning("[SumUp poller] transacao %s sem valor legivel, ignorada", transaction_id)
            continue

        # O endpoint de historico (list_recent_transactions) NAO traz
        # device_info - so' o retrieve de uma transacao especifica traz (ver
        # get_transaction_details). Buscamos sempre, pra sempre verificar
        # qual reader real processou antes de creditar.
        try:
            detalhe = get_transaction_details(access_token, merchant_code, transaction_id)
        except HTTPException as exc:
            logging.warning(
                "[SumUp poller] falha ao buscar detalhe da transacao %s (cliente %s): %s",
                transaction_id, cliente.id, exc.detail,
            )
            detalhe = {}
        device_identifier = _extract_device_identifier(detalhe)
        maquina, motivo = _resolve_maquina(device_identifier, maquina_by_reader_id, single_machine, reader_device_map)
        logging.info(
            "[SumUp poller] transacao %s resolvida: maquina=%s motivo=%s device=%s",
            transaction_id, getattr(maquina, "id_hardware", None), motivo, device_identifier,
        )

        if maquina is None:
            if motivo == "device_sem_reader_correspondente":
                # Device identificado com certeza, mas e' de um reader que NAO
                # esta vinculado a nenhuma maquina nossa - e' uma maquininha
                # fora do CompactPay na mesma conta SumUp do cliente (ex.: usada
                # pra outro negocio). Nao e' uma pendencia a resolver, e' so'
                # uma venda que nao e' nossa - ignora sem gerar alerta/ruido.
                logging.info(
                    "[SumUp poller] transacao %s ignorada: device %s nao pertence a nenhuma maquina do CompactPay",
                    transaction_id, device_identifier,
                )
                continue
            db.add(
                SumupTransacaoPendente(
                    cliente_id=cliente.id,
                    transaction_id=transaction_id,
                    valor=valor,
                    device_identifier=device_identifier,
                    raw_payload=json.dumps(transacao, default=str),
                    created_at=datetime.utcnow(),
                )
            )
            db.commit()
            logging.warning(
                "[SumUp poller] transacao %s (R$%.2f) do cliente %s sem maquina identificada - aguardando resolucao manual",
                transaction_id,
                valor,
                cliente.id,
            )
            continue

        command_id = registrar_pagamento_sumup(
            db,
            machine_id=maquina.id_hardware,
            amount=valor,
            provider_payment_id=transaction_id,
            descricao=f"Pagamento aprovado direto na maquininha SumUp (transaction_id={transaction_id}, terminal_id={maquina.sumup_reader_id})",
            payment_type=transacao.get("card_type") or transacao.get("payment_type"),
        )
        if command_id is None:
            continue
        command_status = liberar_pulso_sumup(maquina.id_hardware, valor, command_id)
        logging.info(
            "[SumUp poller] pagamento standalone processado transaction_id=%s machine=%s amount=%s command_status=%s",
            transaction_id,
            maquina.id_hardware,
            valor,
            command_status,
        )

    cliente.sumup_last_sync_at = datetime.utcnow()
    db.commit()


def poll_all_clientes() -> None:
    db = SessionLocal()
    try:
        clientes = (
            db.query(Cliente)
            .filter(Cliente.sumup_api_key.isnot(None), Cliente.sumup_merchant_code.isnot(None))
            .all()
        )
        for cliente in clientes:
            try:
                process_cliente(db, cliente)
            except Exception:
                db.rollback()
                logging.exception("[SumUp poller] erro processando cliente %s", cliente.id)
    finally:
        db.close()


def run_sumup_poller_worker() -> None:
    logging.info("SumUp poller worker iniciado (intervalo=%ss)", settings.SUMUP_POLLER_INTERVAL_SECONDS)
    while True:
        try:
            poll_all_clientes()
        except Exception:
            logging.exception("[SumUp poller] erro no worker")
        time.sleep(settings.SUMUP_POLLER_INTERVAL_SECONDS)


def start_sumup_poller_worker() -> threading.Thread | None:
    if not settings.START_SUMUP_POLLER_WORKER:
        logging.info("SumUp poller worker desativado por START_SUMUP_POLLER_WORKER=false")
        return None
    thread = threading.Thread(target=run_sumup_poller_worker, daemon=True)
    thread.start()
    return thread

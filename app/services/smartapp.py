"""Banco "mercado_pago_apk": app CompactPay rodando na Point Smart (android-point/).

Fluxo: o app cobra pelo SDK da Point e manda o pagamento para
POST /smartapp/pagamentos. O app NAO e' fonte de verdade: aqui o pagamento e'
reconsultado na API do Mercado Pago com o token do cliente antes de qualquer
pulso. Tudo isto e' separado do webhook (mercado_pago_webhook.py), que segue
cuidando so das maquinas "mercado_pago" - mas usa a mesma trava e a mesma
checagem de duplicidade, entao um payment_id nunca libera credito duas vezes.
"""
import hashlib
import re
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.models import Cliente, EventoTipo, HistoricoOperacao, Maquina, MetodoPagamento, Transacao, VendaPagamento
from app.models.smartapp import TerminalSmartApp
from app.services.command_queue import get_command_status
from app.services.mercado_pago import mp_request
from app.services.mercado_pago_webhook import _acquire_payment_lock
from app.services.mqtt_commands import publish_machine_credit_pulses, publish_machine_ping
from app.services.pagamentos_helpers import (
    calcular_pulsos_por_valor,
    payment_metadata,
    preencher_nome_banco_em_segundo_plano,
)
from app.services.pulse_tracking import update_pulse_status
from app.services.vendas import registrar_venda_pagamento

BANCO_SMARTAPP = "mercado_pago_apk"
ONLINE_SIGNAL_WINDOW = timedelta(seconds=150)  # mesma janela de maquinas_relatorio
PING_TIMEOUT_SECONDS = 6
PAGAMENTO_IDADE_MAXIMA = timedelta(hours=2)
STATUS_APROVADOS = {"approved", "authorized"}


# ---- Chave do terminal --------------------------------------------------

def gerar_chave() -> str:
    return "cpt_" + secrets.token_urlsafe(32)


def hash_chave(chave: str) -> str:
    return hashlib.sha256(chave.encode("utf-8")).hexdigest()


def autenticar_terminal(db: Session, chave: str | None) -> TerminalSmartApp:
    if not chave:
        raise HTTPException(status_code=401, detail="Terminal sem chave")
    terminal = (
        db.query(TerminalSmartApp)
        .filter(TerminalSmartApp.chave_hash == hash_chave(chave), TerminalSmartApp.ativo.is_(True))
        .first()
    )
    if not terminal:
        raise HTTPException(status_code=401, detail="Chave do terminal invalida")
    terminal.ultimo_uso_em = datetime.utcnow()
    db.commit()
    return terminal


def maquina_do_terminal(db: Session, terminal: TerminalSmartApp, machine_id: str) -> Maquina:
    """So maquinas do mesmo cliente e com banco mercado_pago_apk. Qualquer outra
    responde 404, sem revelar que existe."""
    maquina = (
        db.query(Maquina)
        .filter(
            Maquina.id_hardware == machine_id,
            Maquina.cliente_id == terminal.cliente_id,
            Maquina.banco_pagamento == BANCO_SMARTAPP,
        )
        .first()
    )
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada para este terminal")
    return maquina


def maquina_online(maquina: Maquina) -> bool:
    return bool(maquina.ultimo_sinal and datetime.utcnow() - maquina.ultimo_sinal < ONLINE_SIGNAL_WINDOW)


# ---- Placa online ---------------------------------------------------------

def verificar_placa_online(db: Session, maquina: Maquina, timeout: float | None = None) -> bool:
    """Mesmo ping/PONG de POST /maquinas/{id}/verificar-online: manda ping MQTT
    e espera o mqtt_worker gravar PONG/CMD_RECEBIDO no historico."""
    timeout = PING_TIMEOUT_SECONDS if timeout is None else timeout
    machine_id = maquina.id_hardware
    command_id = str(uuid4())
    try:
        publish_machine_ping(machine_id, command_id)
    except Exception:
        return False

    deadline = time.monotonic() + timeout
    responded = False
    while time.monotonic() < deadline:
        db.expire_all()
        response = (
            db.query(HistoricoOperacao)
            .filter(
                HistoricoOperacao.maquina_id == machine_id,
                HistoricoOperacao.categoria == "DISPOSITIVO",
                HistoricoOperacao.command_id == command_id,
                (
                    HistoricoOperacao.descricao.ilike("%status=PONG%")
                    | HistoricoOperacao.descricao.ilike("%status=CMD_RECEBIDO%")
                ),
            )
            .first()
        )
        if response:
            responded = True
            break
        time.sleep(0.25)

    if responded:
        db.refresh(maquina)
        maquina.ultimo_sinal = datetime.utcnow()
        db.commit()
    return responded


# ---- Pagamento ------------------------------------------------------------

def _token_do_cliente(db: Session, cliente_id: int) -> str:
    cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    token = (cliente.mp_access_token or "").strip() if cliente else ""
    if not token:
        raise HTTPException(status_code=422, detail="Cliente sem credencial Mercado Pago")
    return token


def _buscar_pagamento_por_referencia(token: str, payment_reference: str) -> dict | None:
    """O fluxo do SDK nem sempre devolve o payment_id; o app sempre manda a
    description (unica por cobranca). Procura nos pagamentos recentes."""
    query = urllib.parse.urlencode(
        {
            "sort": "date_created",
            "criteria": "desc",
            "range": "date_created",
            "begin_date": "NOW-2HOURS",
            "end_date": "NOW",
            "limit": 50,
        }
    )
    data = mp_request("GET", f"https://api.mercadopago.com/v1/payments/search?{query}", token)
    for payment in data.get("results") or []:
        if (payment.get("description") or "").strip() == payment_reference:
            return payment
    return None


def _parse_mp_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def consultar_pagamento_aprovado(
    token: str,
    maquina: Maquina,
    payment_id: str | None,
    payment_reference: str,
    valor: float,
) -> dict:
    """Devolve o pagamento do MP se ele for valido para ESTA maquina, ou 4xx."""
    # A referencia que o app gera e' CP-{placa}-{timestamp}: amarra o pagamento
    # a esta placa, entao um pagamento de outra maquina nao serve aqui.
    # Formato exato: so prefixo deixaria a placa "X" aceitar a referencia da "X-2".
    if not re.fullmatch(rf"CP-{re.escape(maquina.id_hardware)}-\d+", payment_reference):
        raise HTTPException(status_code=422, detail="Referencia de pagamento invalida para esta maquina")

    if payment_id:
        payment = mp_request("GET", f"https://api.mercadopago.com/v1/payments/{urllib.parse.quote(payment_id)}", token)
    else:
        payment = _buscar_pagamento_por_referencia(token, payment_reference)
        if not payment:
            raise HTTPException(status_code=404, detail="Pagamento ainda nao encontrado no Mercado Pago")

    status = (payment.get("status") or "").lower()
    if status not in STATUS_APROVADOS:
        raise HTTPException(status_code=409, detail=f"Pagamento nao aprovado ({status or 'sem status'})")

    description = (payment.get("description") or "").strip()
    if description and description != payment_reference:
        raise HTTPException(status_code=409, detail="Pagamento nao corresponde a esta cobranca")

    valor_pago = float(payment.get("transaction_amount") or 0)
    if abs(valor_pago - float(valor)) > 0.01:
        raise HTTPException(status_code=409, detail="Valor pago diferente do valor da cobranca")

    criado_em = _parse_mp_datetime(payment.get("date_created"))
    if criado_em and datetime.utcnow() - criado_em > PAGAMENTO_IDADE_MAXIMA:
        raise HTTPException(status_code=409, detail="Pagamento antigo demais para liberar credito")

    return payment


def registrar_pagamento_e_liberar(
    db: Session,
    terminal: TerminalSmartApp,
    maquina: Maquina,
    payment_id: str | None,
    payment_reference: str,
    valor: float,
    metodo: str | None,
    sn_terminal: str | None,
) -> dict:
    token = _token_do_cliente(db, terminal.cliente_id)
    payment = consultar_pagamento_aprovado(token, maquina, payment_id, payment_reference, valor)
    payment_id = str(payment.get("id") or payment_id or "").strip()
    if not payment_id:
        raise HTTPException(status_code=502, detail="Mercado Pago nao retornou o id do pagamento")
    amount = float(payment.get("transaction_amount") or valor)
    machine_id = maquina.id_hardware

    command_id = str(uuid4())
    # Mesma chave de trava do webhook: serializa com uma notificacao do MP
    # para o mesmo pagamento e com retentativas do proprio app.
    _acquire_payment_lock(db, f"mp_payment_{payment_id}")
    duplicado = (
        db.query(VendaPagamento)
        .filter(VendaPagamento.provider == "mercado_pago", VendaPagamento.provider_payment_id == payment_id)
        .first()
    )
    if duplicado:
        db.rollback()  # solta a trava
        if duplicado.maquina_id != machine_id:
            raise HTTPException(status_code=409, detail="Pagamento ja usado em outra maquina")
        # Retentativa do app: devolve o mesmo comando em vez de liberar de novo.
        return {
            "ok": True,
            "duplicado": True,
            "maquina_id": machine_id,
            "payment_id": payment_id,
            "valor": duplicado.valor_bruto,
            "pulsos": calcular_pulsos_por_valor(duplicado.valor_bruto),
            "command_id": duplicado.command_id,
            "command_status": (get_command_status(duplicado.command_id) if duplicado.command_id else None) or "pendente",
        }

    agora = datetime.utcnow()
    transacao = Transacao(
        maquina_id=machine_id,
        tipo=EventoTipo.in_flux,
        metodo=MetodoPagamento.digital,
        valor=amount,
        data_hora=agora,
    )
    db.add(transacao)
    historico = HistoricoOperacao(
        maquina_id=machine_id,
        categoria="PAGAMENTO",
        descricao=(
            f"Pagamento app Point aprovado (payment_id={payment_id}, terminal_id={sn_terminal or 'n/a'}, "
            f"terminal_app={terminal.nome}, metodo={metodo or 'n/a'}, ref={payment_reference})"
        ),
        valor=amount,
        created_at=agora,
        **payment_metadata(payment, token, resolve_bank=False),
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
        created_at=agora,
    )
    db.commit()
    historico_id = historico.id
    nome_banco_pendente = not historico.bank_name

    pulsos = calcular_pulsos_por_valor(amount)
    try:
        publish_machine_credit_pulses(machine_id, pulses=pulsos, action="paid", command_id=command_id, amount=amount)
    except Exception as exc:
        update_pulse_status(command_id, "falha_publicacao")
        raise HTTPException(status_code=502, detail="Falha ao enviar comando MQTT para a maquina") from exc
    finally:
        if nome_banco_pendente:
            preencher_nome_banco_em_segundo_plano(historico_id, payment, token)

    command_status = get_command_status(command_id) or "pendente"
    print(
        f"[SmartApp] pagamento processado payment_id={payment_id} terminal_app={terminal.id} "
        f"machine={machine_id} amount={amount} pulsos={pulsos} command_status={command_status}"
    )
    return {
        "ok": True,
        "duplicado": False,
        "maquina_id": machine_id,
        "payment_id": payment_id,
        "valor": amount,
        "pulsos": pulsos,
        "command_id": command_id,
        "command_status": command_status,
    }

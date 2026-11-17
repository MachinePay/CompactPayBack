"""Tela touch com maquininha SumUp (banco "sumup", maquininha pareada na Cloud API).

Mesmo fluxo da tela com a Point, so que a cobranca vai para a maquininha SumUp
(Reader Checkout da Cloud API). A SumUp nao avisa o backend (sem webhook): a
propria tela consulta a cobranca a cada 2 s e, quando a SumUp diz que foi paga,
o credito e' liberado aqui pelo mesmo processamento de cobranca SumUp que ja
existia (sumup_webhook.processar_callback_sumup). O poller da SumUp continua de
rede de seguranca: ele e o processamento gravam a venda pelo mesmo
transaction_id, com trava, entao o credito sai uma vez so.

So usa as credenciais SumUp cadastradas no proprio cliente (nunca a chave global).
"""
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.models import Cliente, HistoricoOperacao, Maquina, SumupCheckout, VendaPagamento
from app.models.produto import Produto
from app.models.tela_point import TelaPoint
from app.services.command_queue import get_command_status
from app.services.sumup import (
    create_reader,
    create_reader_checkout,
    get_reader_checkout,
    get_reader_status,
    terminate_reader_checkout,
)

BANCO_TELA_SUMUP = "sumup"


def credenciais_sumup(db: Session, cliente_id: int) -> tuple[str, str]:
    cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    token = (cliente.sumup_api_key or "").strip() if cliente else ""
    merchant = (cliente.sumup_merchant_code or "").strip() if cliente else ""
    if not token or not merchant:
        raise HTTPException(status_code=422, detail="Cliente sem credencial SumUp (API key e merchant code)")
    return token, merchant


def reader_da_maquina(maquina: Maquina) -> str:
    reader_id = (maquina.sumup_reader_id or "").strip()
    if not reader_id:
        raise HTTPException(
            status_code=422,
            detail="Maquininha SumUp nao vinculada pela Cloud API nesta maquina. Pareie no painel com o codigo da maquininha.",
        )
    return reader_id


# ---- Painel: parear a maquininha pela Cloud API -----------------------------------

def parear(db: Session, maquina: Maquina, pairing_code: str) -> dict:
    """O codigo e' gerado NA MAQUININHA (Conexoes > API > Conectar) e expira em 5 min."""
    token, merchant = credenciais_sumup(db, maquina.cliente_id)
    reader = create_reader(token, merchant, pairing_code, name=(maquina.nome_local or maquina.id_hardware)[:40])
    maquina.sumup_reader_id = reader["id"]
    if reader.get("device_identifier"):
        maquina.sumup_device_code = reader["device_identifier"]
    db.commit()
    return reader


def situacao_reader(db: Session, maquina: Maquina) -> dict:
    reader_id = (maquina.sumup_reader_id or "").strip()
    if not reader_id:
        return {"reader_id": None, "status": None}
    try:
        token, merchant = credenciais_sumup(db, maquina.cliente_id)
        status = get_reader_status(token, merchant, reader_id)
    except HTTPException as exc:
        status = {"erro": exc.detail}
    return {"reader_id": reader_id, "status": status}


# ---- Cobranca ----------------------------------------------------------------------

def _checkout_da_maquina(db: Session, maquina: Maquina, checkout_id: str) -> SumupCheckout:
    pendente = (
        db.query(SumupCheckout)
        .filter(SumupCheckout.checkout_id == checkout_id, SumupCheckout.maquina_id == maquina.id_hardware)
        .first()
    )
    if not pendente:
        raise HTTPException(status_code=404, detail="Cobranca nao encontrada")
    return pendente


def criar_cobranca(db: Session, tela: TelaPoint, maquina: Maquina, produto: Produto) -> dict:
    token, merchant = credenciais_sumup(db, tela.cliente_id)
    reader_id = reader_da_maquina(maquina)
    valor = round(float(produto.valor), 2)
    checkout = create_reader_checkout(
        token,
        merchant,
        reader_id,
        amount=valor,
        client_transaction_id=f"{maquina.id_hardware}-{uuid4().hex}",
        description=f"{produto.nome or 'Credito'} - {maquina.nome_local or maquina.id_hardware}"[:150],
    )
    checkout_id = checkout["checkout_id"]
    db.add(SumupCheckout(checkout_id=checkout_id, maquina_id=maquina.id_hardware, reader_id=reader_id, valor=valor))
    db.add(
        HistoricoOperacao(
            maquina_id=maquina.id_hardware,
            categoria="COBRANCA_TELA",
            descricao=(
                f"Cobranca enviada pela tela para a maquininha SumUp (checkout_id={checkout_id}, "
                f"reader_id={reader_id}, tela={tela.nome}, produto_id={produto.id})"
            ),
            valor=valor,
            created_at=datetime.utcnow(),
        )
    )
    db.commit()
    print(f"[Tela SumUp] checkout criado {checkout_id} machine={maquina.id_hardware} valor={valor} reader={reader_id}")
    return {"order_id": checkout_id, "status": "created", "valor": valor}


def consultar_cobranca(db: Session, tela: TelaPoint, maquina: Maquina, checkout_id: str) -> dict:
    # Import local: o modulo do webhook importa bastante coisa e so' e' preciso aqui.
    from app.services.sumup_webhook import FAILED_STATUSES, SUCCESS_STATUSES, processar_callback_sumup

    pendente = _checkout_da_maquina(db, maquina, checkout_id)
    token, merchant = credenciais_sumup(db, tela.cliente_id)
    dados = get_reader_checkout(token, merchant, pendente.reader_id, checkout_id)
    status = str(dados.get("status") or "").strip().lower()

    if status in SUCCESS_STATUSES:
        if not pendente.processado:
            # Sem webhook na SumUp: a consulta da tela e' quem dispara o credito.
            processar_callback_sumup({"id": checkout_id})
            db.expire_all()
        transaction_id = str(dados.get("transaction_id") or checkout_id)
        venda = (
            db.query(VendaPagamento)
            .filter(VendaPagamento.provider == "sumup", VendaPagamento.provider_payment_id == transaction_id)
            .first()
        )
        command_id = venda.command_id if venda else None
        return {
            "order_id": checkout_id,
            "situacao": "pago",
            "status_mp": status,
            "command_id": command_id,
            "command_status": (get_command_status(command_id) if command_id else None) or "pendente",
        }
    situacao = "cancelado" if status in FAILED_STATUSES else "aguardando"
    return {"order_id": checkout_id, "situacao": situacao, "status_mp": status}


def cancelar_cobranca(db: Session, tela: TelaPoint, maquina: Maquina, checkout_id: str) -> dict:
    pendente = _checkout_da_maquina(db, maquina, checkout_id)
    token, merchant = credenciais_sumup(db, tela.cliente_id)
    terminate_reader_checkout(token, merchant, pendente.reader_id)
    return {"order_id": checkout_id, "status_mp": "cancelado"}

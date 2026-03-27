from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import EventoTipo, HistoricoOperacao, Maquina, MetodoPagamento, Transacao
from app.models.produto import Produto
from app.schemas.pagamento import PagamentoCreate, PagamentoOut
from app.services.mqtt_commands import publish_machine_credit

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_maquina_visivel(db: Session, maquina_id: str, role: str, cliente_id):
    query = db.query(Maquina).filter(Maquina.id_hardware == maquina_id)
    if role != "admin":
        query = query.filter(Maquina.cliente_id == cliente_id)
    maquina = query.first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")
    return maquina


@router.post("/callback-mercado-pago")
async def processar_pix(dados: dict):
    pago = dados.get("status") == "approved"
    id_hardware = dados.get("id_hardware")
    valor = float(dados.get("valor", 1.0))
    if not (pago and id_hardware):
        return {"status": "erro", "detalhe": "Pix nao aprovado ou maquina nao informada"}

    db = SessionLocal()
    nova_transacao = Transacao(
        maquina_id=id_hardware,
        tipo=EventoTipo.in_flux,
        metodo=MetodoPagamento.digital,
        valor=valor,
        data_hora=datetime.utcnow(),
    )
    db.add(nova_transacao)
    db.commit()
    db.close()

    return {"status": "sucesso", "detalhe": "Pagamento digital registrado"}


@router.post("/pagamentos/lancar", response_model=PagamentoOut)
def lancar_pagamento(
    pagamento: PagamentoCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    _get_maquina_visivel(db, pagamento.maquina_id, role, cliente_id)

    if pagamento.produto_id is not None:
        produto = db.query(Produto).filter(Produto.id == pagamento.produto_id).first()
        if not produto:
            raise HTTPException(status_code=404, detail="Produto nao encontrado")
        if produto.maquina_id != pagamento.maquina_id:
            raise HTTPException(status_code=400, detail="Produto nao pertence a maquina informada")

    transacao = Transacao(
        maquina_id=pagamento.maquina_id,
        tipo=EventoTipo.in_flux,
        metodo=MetodoPagamento.digital,
        valor=pagamento.valor,
        data_hora=datetime.utcnow(),
    )
    db.add(transacao)
    db.add(
        HistoricoOperacao(
            maquina_id=pagamento.maquina_id,
            categoria="PAGAMENTO",
            descricao=pagamento.descricao or "Pagamento digital lancado pelo painel",
            valor=pagamento.valor,
            created_at=transacao.data_hora,
        )
    )
    db.commit()
    db.refresh(transacao)

    try:
        payload = publish_machine_credit(pagamento.maquina_id, action="paid")
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Falha ao enviar comando MQTT para a maquina") from exc

    return {
        "ok": True,
        "maquina_id": pagamento.maquina_id,
        "valor": pagamento.valor,
        "produto_id": pagamento.produto_id,
        "payload": payload,
        "data_hora": transacao.data_hora,
    }

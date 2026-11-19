"""Resumo por cliente para a tela de Usuarios (admin).

Devolve, para cada cliente, quantidade de maquinas, status SumUp e a ultima
venda. Tudo em consultas agrupadas por cliente (sem N+1): a tela junta este
resumo com GET /usuarios pelo cliente_id.

Venda = VendaPagamento que nao e' teste, ou Transacao tipo IN (moeda/nota
fisica e registros antigos que nao tem VendaPagamento).
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import Cliente, EventoTipo, Maquina, Transacao, VendaPagamento

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def montar_resumo_clientes(db: Session) -> list[dict]:
    clientes = db.query(
        Cliente.id,
        Cliente.cliente_sumup,
        Cliente.sumup_api_key,
        Cliente.sumup_merchant_code,
    ).all()

    maquinas_por_cliente = dict(
        db.query(Maquina.cliente_id, func.count(Maquina.id_hardware))
        .filter(Maquina.cliente_id.isnot(None))
        .group_by(Maquina.cliente_id)
        .all()
    )

    ultima_venda_pag = dict(
        db.query(Maquina.cliente_id, func.max(VendaPagamento.created_at))
        .join(Maquina, Maquina.id_hardware == VendaPagamento.maquina_id)
        .filter(Maquina.cliente_id.isnot(None), VendaPagamento.is_teste.is_(False))
        .group_by(Maquina.cliente_id)
        .all()
    )

    ultima_transacao_in = dict(
        db.query(Maquina.cliente_id, func.max(Transacao.data_hora))
        .join(Maquina, Maquina.id_hardware == Transacao.maquina_id)
        .filter(Maquina.cliente_id.isnot(None), Transacao.tipo == EventoTipo.in_flux)
        .group_by(Maquina.cliente_id)
        .all()
    )

    resumo = []
    for cliente_id, cliente_sumup, sumup_api_key, sumup_merchant_code in clientes:
        datas = [d for d in (ultima_venda_pag.get(cliente_id), ultima_transacao_in.get(cliente_id)) if d]
        ultima_venda = max(datas) if datas else None
        resumo.append(
            {
                "cliente_id": cliente_id,
                "maquinas": int(maquinas_por_cliente.get(cliente_id, 0)),
                "cliente_sumup": bool(cliente_sumup),
                "sumup_configurado": bool(sumup_api_key and sumup_merchant_code),
                "tem_venda": ultima_venda is not None,
                # Datas gravadas em UTC (datetime.utcnow); o "Z" deixa o
                # navegador converter para o horario local.
                "ultima_venda": ultima_venda.isoformat() + "Z" if ultima_venda else None,
            }
        )
    return resumo


@router.get("/usuarios/resumo-clientes")
def resumo_clientes(db: Session = Depends(get_db), user=Depends(get_current_user)):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin")
    return montar_resumo_clientes(db)

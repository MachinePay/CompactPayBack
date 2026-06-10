from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import Cliente
from app.schemas.cliente import ClienteListOut

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _cliente_query_por_usuario(db: Session, role: str, cliente_id):
    query = db.query(Cliente)
    if role == "admin":
        return query
    return query.filter(Cliente.id == cliente_id)


@router.get("/clientes", response_model=List[ClienteListOut])
def listar_clientes(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    clientes = (
        _cliente_query_por_usuario(db, role, cliente_id)
        .order_by(Cliente.nome_empresa.asc())
        .all()
    )
    return [
        {
            "id": cliente.id,
            "nome_empresa": cliente.nome_empresa,
            "email_contato": cliente.email_contato,
            "telefone": cliente.telefone,
            "cpf": cliente.cpf,
            "cnpj": cliente.cnpj,
            "endereco_rua": cliente.endereco_rua,
            "endereco_numero": cliente.endereco_numero,
            "endereco_cidade": cliente.endereco_cidade,
            "endereco_estado": cliente.endereco_estado,
            "endereco_latitude": cliente.endereco_latitude,
            "endereco_longitude": cliente.endereco_longitude,
            "cliente_mercado_pago": bool(cliente.cliente_mercado_pago or cliente.mp_access_token),
            "cliente_pagbank": bool(cliente.cliente_pagbank),
            "cliente_s6pay": bool(cliente.cliente_s6pay),
            "mp_configurado": bool(cliente.mp_access_token),
            "mp_pos_category": cliente.mp_pos_category,
            "mp_user_id": cliente.mp_user_id,
            "mp_store_id": cliente.mp_store_id,
            "mp_store_external_id": cliente.mp_store_external_id,
        }
        for cliente in clientes
    ]

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import Cliente
from app.schemas.cliente import ClienteListOut
from app.services.sumup import create_reader, list_readers

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
            "cliente_token_play": bool(cliente.cliente_token_play),
            "mp_configurado": bool(cliente.mp_access_token),
            "mp_pos_category": cliente.mp_pos_category,
            "mp_user_id": cliente.mp_user_id,
            "mp_store_id": cliente.mp_store_id,
            "mp_store_external_id": cliente.mp_store_external_id,
            "cliente_sumup": bool(cliente.cliente_sumup or (cliente.sumup_api_key and cliente.sumup_merchant_code)),
            "sumup_configurado": bool(cliente.sumup_api_key and cliente.sumup_merchant_code),
        }
        for cliente in clientes
    ]


@router.get("/clientes/{cliente_id}/sumup/readers")
def listar_sumup_readers(
    cliente_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Lista os readers (maquininhas) ja pareados na conta SumUp do cliente,
    pra escolher qual vincular a uma maquina nova (ver MaquinaCreate.sumup_reader_id).
    Diferente do Mercado Pago, o SumUp nao cria reader por API - precisa estar
    pareado antes, pelo app SumUp."""
    _, role, logged_cliente_id = user
    if role != "admin" and cliente_id != logged_cliente_id:
        raise HTTPException(status_code=403, detail="Sem permissao para ver readers deste cliente")
    cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    access_token = (cliente.sumup_api_key or "").strip()
    merchant_code = (cliente.sumup_merchant_code or "").strip()
    if not access_token or not merchant_code:
        raise HTTPException(status_code=422, detail="Cliente sem SUMUP_API_KEY/SUMUP_MERCHANT_CODE cadastrados")
    return list_readers(access_token, merchant_code)


@router.post("/clientes/{cliente_id}/sumup/readers")
def parear_sumup_reader(
    cliente_id: int,
    dados: dict,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Registra um reader na Cloud API da SumUp a partir do pairing_code
    gerado NO APARELHO (menu > Connections > conecta no Wi-Fi > API > Connect
    - o codigo expira em 5 minutos). So depois disso o reader passa a
    aparecer em listar_sumup_readers (dropdown de selecao)."""
    _, role, logged_cliente_id = user
    if role != "admin" and cliente_id != logged_cliente_id:
        raise HTTPException(status_code=403, detail="Sem permissao para parear readers deste cliente")
    cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    access_token = (cliente.sumup_api_key or "").strip()
    merchant_code = (cliente.sumup_merchant_code or "").strip()
    if not access_token or not merchant_code:
        raise HTTPException(status_code=422, detail="Cliente sem SUMUP_API_KEY/SUMUP_MERCHANT_CODE cadastrados")
    pairing_code = (dados.get("pairing_code") or "").strip()
    if not pairing_code:
        raise HTTPException(status_code=422, detail="pairing_code e obrigatorio")
    nome = (dados.get("name") or "").strip() or None
    return create_reader(access_token, merchant_code, pairing_code, name=nome)

from typing import List
import secrets

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.security import get_password_hash
from app.db.session import SessionLocal
from app.models.models import Cliente, UserRole, Usuario
from app.schemas.usuario import UsuarioCreate, UsuarioOut, UsuarioUpdate

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _resolve_user_role(role: str) -> UserRole:
    try:
        return UserRole(role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Perfil invalido") from exc


def _create_cliente_for_user(db: Session, usuario: UsuarioCreate | UsuarioUpdate) -> Cliente:
    email = str(usuario.email)
    base_name = (usuario.nome or email.split("@")[0]).strip() or "Cliente CompactPay"
    cliente = Cliente(
        nome_empresa=base_name,
        email_contato=str(usuario.email),
        api_key=secrets.token_hex(16),
        telefone=usuario.telefone,
        cpf=usuario.cpf,
        cnpj=usuario.cnpj,
        endereco_rua=usuario.endereco_rua,
        endereco_numero=usuario.endereco_numero,
        endereco_cidade=usuario.endereco_cidade,
        endereco_estado=usuario.endereco_estado,
        endereco_latitude=usuario.endereco_latitude,
        endereco_longitude=usuario.endereco_longitude,
        cliente_mercado_pago=bool(usuario.cliente_mercado_pago),
        cliente_pagbank=bool(usuario.cliente_pagbank),
        cliente_s6pay=bool(usuario.cliente_s6pay),
        mp_public_key=usuario.mp_public_key,
        mp_access_token=usuario.mp_access_token,
        mp_client_id=usuario.mp_client_id,
        mp_client_secret=usuario.mp_client_secret,
        mp_user_id=usuario.mp_user_id,
        mp_pos_category=usuario.mp_pos_category,
        mp_store_id=usuario.mp_store_id,
        mp_store_external_id=usuario.mp_store_external_id,
    )
    db.add(cliente)
    db.flush()
    return cliente


def _sync_cliente_from_usuario(db: Session, usuario: UsuarioCreate | UsuarioUpdate, cliente_id: int | None) -> None:
    if cliente_id is None:
        return
    cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    if not cliente:
        return
    cliente.nome_empresa = usuario.nome or cliente.nome_empresa
    cliente.email_contato = str(usuario.email)
    cliente.telefone = usuario.telefone
    cliente.cpf = usuario.cpf
    cliente.cnpj = usuario.cnpj
    cliente.endereco_rua = usuario.endereco_rua
    cliente.endereco_numero = usuario.endereco_numero
    cliente.endereco_cidade = usuario.endereco_cidade
    cliente.endereco_estado = usuario.endereco_estado
    cliente.endereco_latitude = usuario.endereco_latitude
    cliente.endereco_longitude = usuario.endereco_longitude
    cliente.cliente_mercado_pago = bool(usuario.cliente_mercado_pago)
    cliente.cliente_pagbank = bool(usuario.cliente_pagbank)
    cliente.cliente_s6pay = bool(usuario.cliente_s6pay)
    if not usuario.cliente_mercado_pago:
        cliente.mp_public_key = None
        cliente.mp_access_token = None
        cliente.mp_client_id = None
        cliente.mp_client_secret = None
        cliente.mp_user_id = None
        cliente.mp_pos_category = None
        cliente.mp_store_id = None
        cliente.mp_store_external_id = None
        return
    cliente.mp_public_key = usuario.mp_public_key
    if usuario.mp_access_token and usuario.mp_access_token != "********":
        cliente.mp_access_token = usuario.mp_access_token
    cliente.mp_client_id = usuario.mp_client_id
    if usuario.mp_client_secret and usuario.mp_client_secret != "********":
        cliente.mp_client_secret = usuario.mp_client_secret
    cliente.mp_user_id = usuario.mp_user_id
    cliente.mp_pos_category = usuario.mp_pos_category
    cliente.mp_store_id = usuario.mp_store_id
    cliente.mp_store_external_id = usuario.mp_store_external_id


def _sync_db_usuario_fields(db_usuario: Usuario, usuario: UsuarioCreate | UsuarioUpdate) -> None:
    db_usuario.nome = usuario.nome
    db_usuario.telefone = usuario.telefone
    db_usuario.cpf = usuario.cpf
    db_usuario.cnpj = usuario.cnpj
    db_usuario.endereco_rua = usuario.endereco_rua
    db_usuario.endereco_numero = usuario.endereco_numero
    db_usuario.endereco_cidade = usuario.endereco_cidade
    db_usuario.endereco_estado = usuario.endereco_estado
    db_usuario.endereco_latitude = usuario.endereco_latitude
    db_usuario.endereco_longitude = usuario.endereco_longitude
    db_usuario.email = usuario.email
    db_usuario.cliente_mercado_pago = bool(usuario.cliente_mercado_pago)
    db_usuario.cliente_pagbank = bool(usuario.cliente_pagbank)
    db_usuario.cliente_s6pay = bool(usuario.cliente_s6pay)
    if not usuario.cliente_mercado_pago:
        db_usuario.mp_public_key = None
        db_usuario.mp_access_token = None
        db_usuario.mp_client_id = None
        db_usuario.mp_client_secret = None
        db_usuario.mp_user_id = None
        db_usuario.mp_pos_category = None
        db_usuario.mp_store_id = None
        db_usuario.mp_store_external_id = None
        return
    db_usuario.mp_public_key = usuario.mp_public_key
    if usuario.mp_access_token and usuario.mp_access_token != "********":
        db_usuario.mp_access_token = usuario.mp_access_token
    db_usuario.mp_client_id = usuario.mp_client_id
    if usuario.mp_client_secret and usuario.mp_client_secret != "********":
        db_usuario.mp_client_secret = usuario.mp_client_secret
    db_usuario.mp_user_id = usuario.mp_user_id
    db_usuario.mp_pos_category = usuario.mp_pos_category
    db_usuario.mp_store_id = usuario.mp_store_id
    db_usuario.mp_store_external_id = usuario.mp_store_external_id


def _serialize_usuario(db_usuario: Usuario) -> dict:
    cliente = getattr(db_usuario, "cliente", None)
    return {
        "id": db_usuario.id,
        "email": db_usuario.email,
        "role": db_usuario.role.value if hasattr(db_usuario.role, "value") else str(db_usuario.role),
        "cliente_id": db_usuario.cliente_id,
        "nome": db_usuario.nome or (cliente.nome_empresa if cliente else None),
        "telefone": db_usuario.telefone or (cliente.telefone if cliente else None),
        "cpf": db_usuario.cpf or (cliente.cpf if cliente else None),
        "cnpj": db_usuario.cnpj or (cliente.cnpj if cliente else None),
        "endereco_rua": db_usuario.endereco_rua or (cliente.endereco_rua if cliente else None),
        "endereco_numero": db_usuario.endereco_numero or (cliente.endereco_numero if cliente else None),
        "endereco_cidade": db_usuario.endereco_cidade or (cliente.endereco_cidade if cliente else None),
        "endereco_estado": db_usuario.endereco_estado or (cliente.endereco_estado if cliente else None),
        "endereco_latitude": db_usuario.endereco_latitude if db_usuario.endereco_latitude is not None else (cliente.endereco_latitude if cliente else None),
        "endereco_longitude": db_usuario.endereco_longitude if db_usuario.endereco_longitude is not None else (cliente.endereco_longitude if cliente else None),
        "cliente_mercado_pago": bool(db_usuario.cliente_mercado_pago or (cliente and cliente.cliente_mercado_pago)),
        "cliente_pagbank": bool(db_usuario.cliente_pagbank or (cliente and cliente.cliente_pagbank)),
        "cliente_s6pay": bool(db_usuario.cliente_s6pay or (cliente and cliente.cliente_s6pay)),
        "mp_public_key": db_usuario.mp_public_key or (cliente.mp_public_key if cliente else None),
        "mp_access_token": "********" if db_usuario.mp_access_token or (cliente and cliente.mp_access_token) else None,
        "mp_client_id": db_usuario.mp_client_id or (cliente.mp_client_id if cliente else None),
        "mp_client_secret": "********" if db_usuario.mp_client_secret or (cliente and cliente.mp_client_secret) else None,
        "mp_user_id": db_usuario.mp_user_id or (cliente.mp_user_id if cliente else None),
        "mp_store_id": db_usuario.mp_store_id or (cliente.mp_store_id if cliente else None),
        "mp_store_external_id": db_usuario.mp_store_external_id or (cliente.mp_store_external_id if cliente else None),
        "mp_live_mode": db_usuario.mp_live_mode if db_usuario.mp_live_mode is not None else (cliente.mp_live_mode if cliente else None),
        "mp_scope": db_usuario.mp_scope or (cliente.mp_scope if cliente else None),
        "mp_pos_category": db_usuario.mp_pos_category or (cliente.mp_pos_category if cliente else None),
        "mp_configurado": bool(db_usuario.mp_access_token or (cliente and cliente.mp_access_token)),
    }


def _validate_cliente_id(db: Session, role: UserRole, cliente_id: int | None) -> int | None:
    if role == UserRole.admin:
        return None
    if cliente_id is None:
        return None
    cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    if not cliente:
        raise HTTPException(status_code=400, detail="Cliente ID invalido")
    return cliente_id


@router.post("/usuarios", response_model=UsuarioOut)
def criar_usuario(
    usuario: UsuarioCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode criar usuarios")
    if db.query(Usuario).filter(Usuario.email == usuario.email).first():
        raise HTTPException(status_code=400, detail="Email ja cadastrado")

    user_role = _resolve_user_role(usuario.role)
    cliente_id = _validate_cliente_id(db, user_role, usuario.cliente_id)
    if user_role == UserRole.cliente and cliente_id is None:
        cliente_id = _create_cliente_for_user(db, usuario).id
    elif user_role == UserRole.cliente:
        _sync_cliente_from_usuario(db, usuario, cliente_id)

    db_usuario = Usuario(
        email=usuario.email,
        hashed_password=get_password_hash(usuario.password),
        role=user_role,
        cliente_id=cliente_id,
    )
    _sync_db_usuario_fields(db_usuario, usuario)
    db.add(db_usuario)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Nao foi possivel criar o usuario com os dados informados") from exc
    db.refresh(db_usuario)
    return _serialize_usuario(db_usuario)


@router.get("/usuarios", response_model=List[UsuarioOut])
def listar_usuarios(db: Session = Depends(get_db), user=Depends(get_current_user)):
    _, role, cliente_id = user
    if role == "admin":
        usuarios = db.query(Usuario).all()
    else:
        usuarios = db.query(Usuario).filter(Usuario.cliente_id == cliente_id).all()
    return [_serialize_usuario(item) for item in usuarios]


@router.put("/usuarios/{usuario_id}", response_model=UsuarioOut)
def atualizar_usuario(
    usuario_id: int,
    usuario: UsuarioUpdate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode atualizar usuarios")

    db_usuario = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    if not db_usuario:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado")

    user_role = _resolve_user_role(usuario.role)
    cliente_id = _validate_cliente_id(db, user_role, usuario.cliente_id)
    if user_role == UserRole.cliente and cliente_id is None:
        cliente_id = db_usuario.cliente_id
        if cliente_id is None:
            cliente_id = _create_cliente_for_user(db, usuario).id
    if user_role == UserRole.cliente:
        _sync_cliente_from_usuario(db, usuario, cliente_id)

    _sync_db_usuario_fields(db_usuario, usuario)
    db_usuario.role = user_role
    db_usuario.cliente_id = cliente_id
    if usuario.password:
        db_usuario.hashed_password = get_password_hash(usuario.password)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Nao foi possivel atualizar o usuario com os dados informados") from exc
    db.refresh(db_usuario)
    return _serialize_usuario(db_usuario)


@router.delete("/usuarios/{usuario_id}")
def deletar_usuario(
    usuario_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode deletar usuarios")

    db_usuario = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    if not db_usuario:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado")

    db.delete(db_usuario)
    db.commit()
    return {"ok": True}

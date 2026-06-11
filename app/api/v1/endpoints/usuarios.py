from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.security import get_password_hash
from app.db.session import SessionLocal
from app.models.models import UserRole, Usuario
from app.schemas.usuario import UsuarioCreate, UsuarioOut, UsuarioUpdate
from app.services.auditoria import registrar_auditoria
from app.services.usuarios_helpers import (
    create_cliente_for_user,
    resolve_user_role,
    serialize_usuario,
    sync_cliente_from_usuario,
    sync_db_usuario_fields,
    validate_cliente_id,
)

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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

    user_role = resolve_user_role(usuario.role)
    cliente_id = validate_cliente_id(db, user_role, usuario.cliente_id)
    if user_role == UserRole.cliente and cliente_id is None:
        cliente_id = create_cliente_for_user(db, usuario).id
    elif user_role == UserRole.cliente:
        sync_cliente_from_usuario(db, usuario, cliente_id)

    db_usuario = Usuario(
        email=usuario.email,
        hashed_password=get_password_hash(usuario.password),
        role=user_role,
        cliente_id=cliente_id,
    )
    sync_db_usuario_fields(db_usuario, usuario)
    db.add(db_usuario)
    try:
        db.flush()
        registrar_auditoria(
            db,
            user,
            acao="USUARIO_CRIADO",
            entidade_tipo="usuario",
            entidade_id=db_usuario.id,
            descricao=f"Usuario criado email={usuario.email} role={user_role.value}",
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Nao foi possivel criar o usuario com os dados informados") from exc
    db.refresh(db_usuario)
    return serialize_usuario(db_usuario)


@router.get("/usuarios", response_model=List[UsuarioOut])
def listar_usuarios(db: Session = Depends(get_db), user=Depends(get_current_user)):
    _, role, cliente_id = user
    if role == "admin":
        usuarios = db.query(Usuario).all()
    else:
        usuarios = db.query(Usuario).filter(Usuario.cliente_id == cliente_id).all()
    return [serialize_usuario(item) for item in usuarios]


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

    user_role = resolve_user_role(usuario.role)
    cliente_id = validate_cliente_id(db, user_role, usuario.cliente_id)
    if user_role == UserRole.cliente and cliente_id is None:
        cliente_id = db_usuario.cliente_id
        if cliente_id is None:
            cliente_id = create_cliente_for_user(db, usuario).id
    if user_role == UserRole.cliente:
        sync_cliente_from_usuario(db, usuario, cliente_id)

    sync_db_usuario_fields(db_usuario, usuario)
    db_usuario.role = user_role
    db_usuario.cliente_id = cliente_id
    if usuario.password:
        db_usuario.hashed_password = get_password_hash(usuario.password)
    registrar_auditoria(
        db,
        user,
        acao="USUARIO_ATUALIZADO",
        entidade_tipo="usuario",
        entidade_id=usuario_id,
        descricao=(
            f"Usuario atualizado email={usuario.email} role={user_role.value} "
            f"cliente_id={cliente_id} senha_alterada={bool(usuario.password)} "
            f"mp_habilitado={bool(usuario.cliente_mercado_pago)} pagbank={bool(usuario.cliente_pagbank)} s6pay={bool(usuario.cliente_s6pay)}"
        ),
    )

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Nao foi possivel atualizar o usuario com os dados informados") from exc
    db.refresh(db_usuario)
    return serialize_usuario(db_usuario)


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

    email = db_usuario.email
    role_value = db_usuario.role.value if hasattr(db_usuario.role, "value") else str(db_usuario.role)
    cliente_id = db_usuario.cliente_id
    registrar_auditoria(
        db,
        user,
        acao="USUARIO_EXCLUIDO",
        entidade_tipo="usuario",
        entidade_id=usuario_id,
        descricao=f"Usuario excluido email={email} role={role_value} cliente_id={cliente_id}",
    )
    db.delete(db_usuario)
    db.commit()
    return {"ok": True}

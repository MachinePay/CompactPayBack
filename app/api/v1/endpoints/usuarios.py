from typing import List

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


def _validate_cliente_id(db: Session, role: UserRole, cliente_id: int | None) -> int | None:
    if role == UserRole.admin:
        return None
    if cliente_id is None:
        raise HTTPException(status_code=400, detail="Cliente ID obrigatorio para perfil cliente")
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

    db_usuario = Usuario(
        email=usuario.email,
        hashed_password=get_password_hash(usuario.password),
        role=user_role,
        cliente_id=cliente_id,
    )
    db.add(db_usuario)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Nao foi possivel criar o usuario com os dados informados") from exc
    db.refresh(db_usuario)
    return db_usuario


@router.get("/usuarios", response_model=List[UsuarioOut])
def listar_usuarios(db: Session = Depends(get_db), user=Depends(get_current_user)):
    _, role, cliente_id = user
    if role == "admin":
        return db.query(Usuario).all()
    return db.query(Usuario).filter(Usuario.cliente_id == cliente_id).all()


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

    db_usuario.email = usuario.email
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
    return db_usuario


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

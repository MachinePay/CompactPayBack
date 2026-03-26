from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.security import get_password_hash
from app.db.session import SessionLocal
from app.models.models import Usuario
from app.schemas.usuario import UsuarioCreate, UsuarioOut, UsuarioUpdate

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

    db_usuario = Usuario(
        email=usuario.email,
        hashed_password=get_password_hash(usuario.password),
        role=usuario.role,
        cliente_id=usuario.cliente_id,
    )
    db.add(db_usuario)
    db.commit()
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

    db_usuario.email = usuario.email
    db_usuario.role = usuario.role
    db_usuario.cliente_id = usuario.cliente_id
    if usuario.password:
        db_usuario.hashed_password = get_password_hash(usuario.password)

    db.commit()
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

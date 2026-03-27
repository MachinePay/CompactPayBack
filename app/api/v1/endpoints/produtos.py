from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import Maquina, Usuario
from app.models.produto import Produto
from app.schemas.produto import ProdutoCreate, ProdutoOut

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_usuario_autenticado(db: Session, user) -> Usuario:
    token_data, _, _ = user
    db_usuario = db.query(Usuario).filter(Usuario.email == token_data.email).first()
    if not db_usuario:
        raise HTTPException(status_code=401, detail="Usuario autenticado nao encontrado")
    return db_usuario


def _get_maquina_visivel(db: Session, maquina_id: str, role: str, cliente_id):
    query = db.query(Maquina).filter(Maquina.id_hardware == maquina_id)
    if role != "admin":
        query = query.filter(Maquina.cliente_id == cliente_id)
    maquina = query.first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")
    return maquina


def _produto_out(produto: Produto, maquina_nome: str | None = None):
    return {
        "id": produto.id,
        "nome": produto.nome,
        "valor": float(produto.valor),
        "maquina_id": produto.maquina_id,
        "usuario_id": produto.usuario_id,
        "maquina_nome": maquina_nome,
    }


@router.post("/produtos", response_model=ProdutoOut)
def criar_produto(
    produto: ProdutoCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    db_usuario = _get_usuario_autenticado(db, user)
    maquina = _get_maquina_visivel(db, produto.maquina_id, role, cliente_id)

    db_produto = Produto(
        nome=produto.nome,
        valor=produto.valor,
        maquina_id=produto.maquina_id,
        usuario_id=db_usuario.id,
    )
    db.add(db_produto)
    db.commit()
    db.refresh(db_produto)
    return _produto_out(db_produto, maquina.nome_local)


@router.get("/produtos", response_model=List[ProdutoOut])
def listar_produtos(
    maquina_id: str = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    query = db.query(Produto)
    if role != "admin":
        query = query.join(Maquina, Produto.maquina_id == Maquina.id_hardware).filter(
            Maquina.cliente_id == cliente_id
        )
    if maquina_id:
        query = query.filter(Produto.maquina_id == maquina_id)

    produtos = query.all()
    maquinas_ids = list({produto.maquina_id for produto in produtos})
    maquinas = {
        maquina.id_hardware: maquina.nome_local
        for maquina in db.query(Maquina).filter(Maquina.id_hardware.in_(maquinas_ids)).all()
    }
    return [_produto_out(produto, maquinas.get(produto.maquina_id)) for produto in produtos]


@router.put("/produtos/{produto_id}", response_model=ProdutoOut)
def atualizar_produto(
    produto_id: int,
    produto: ProdutoCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    db_produto = db.query(Produto).filter(Produto.id == produto_id).first()
    if not db_produto:
        raise HTTPException(status_code=404, detail="Produto nao encontrado")

    maquina = _get_maquina_visivel(db, produto.maquina_id, role, cliente_id)
    _get_maquina_visivel(db, db_produto.maquina_id, role, cliente_id)

    db_produto.nome = produto.nome
    db_produto.valor = produto.valor
    db_produto.maquina_id = produto.maquina_id
    db.commit()
    db.refresh(db_produto)
    return _produto_out(db_produto, maquina.nome_local)


@router.delete("/produtos/{produto_id}")
def deletar_produto(
    produto_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    db_produto = db.query(Produto).filter(Produto.id == produto_id).first()
    if not db_produto:
        raise HTTPException(status_code=404, detail="Produto nao encontrado")

    _get_maquina_visivel(db, db_produto.maquina_id, role, cliente_id)
    db.delete(db_produto)
    db.commit()
    return {"ok": True}

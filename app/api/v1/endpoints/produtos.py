from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.models.produto import Produto
from app.schemas.produto import ProdutoCreate, ProdutoOut
from app.core.dependencies import get_current_user
from typing import List

router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/produtos", response_model=ProdutoOut)
def criar_produto(produto: ProdutoCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _, role, cliente_id = user
    db_produto = Produto(**produto.dict())
    db.add(db_produto)
    db.commit()
    db.refresh(db_produto)
    return db_produto

@router.get("/produtos", response_model=List[ProdutoOut])
def listar_produtos(maquina_id: str = None, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _, role, cliente_id = user
    query = db.query(Produto)
    if maquina_id:
        query = query.filter(Produto.maquina_id == maquina_id)
    return query.all()

@router.put("/produtos/{produto_id}", response_model=ProdutoOut)
def atualizar_produto(produto_id: int, produto: ProdutoCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    db_produto = db.query(Produto).filter(Produto.id == produto_id).first()
    if not db_produto:
        raise HTTPException(status_code=404, detail="Produto não encontrado")
    for key, value in produto.dict().items():
        setattr(db_produto, key, value)
    db.commit()
    db.refresh(db_produto)
    return db_produto

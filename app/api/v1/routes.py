from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.schemas.maquinas import MaquinaOut
from app.schemas.transacoes import TransacaoOut
from app.models.maquinas import Maquinas
from app.models.transacoes import Transacoes
from typing import List

router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/maquinas", response_model=List[MaquinaOut])
def listar_maquinas(db: Session = Depends(get_db)):
    return db.query(Maquinas).all()

@router.get("/transacoes", response_model=List[TransacaoOut])
def listar_transacoes(db: Session = Depends(get_db)):
    return db.query(Transacoes).all()

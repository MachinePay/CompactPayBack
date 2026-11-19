"""Nome do ponto e localizacao da maquina editados pelo proprio cliente.

O PUT /maquinas/{id} (maquinas.py) continua so do admin e mexe em tudo. Aqui
o cliente dono da maquina (ou o admin) altera SO nome_local e localizacao -
qualquer outro campo do corpo e' ignorado. Maquina de outro cliente da 404.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.escopo import obter_maquina_do_usuario
from app.db.session import SessionLocal
from app.services.auditoria import registrar_auditoria

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class PontoUpdate(BaseModel):
    nome: str
    localizacao: Optional[str] = None


@router.patch("/maquinas/{machine_id}/ponto")
def atualizar_ponto_da_maquina(
    machine_id: str,
    dados: PontoUpdate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    maquina = obter_maquina_do_usuario(db, user, machine_id)
    nome = (dados.nome or "").strip()
    if not nome:
        raise HTTPException(status_code=422, detail="Informe o nome do ponto")
    if len(nome) > 120:
        raise HTTPException(status_code=422, detail="Nome do ponto muito longo (max. 120 caracteres)")
    localizacao = (dados.localizacao or "").strip() or None
    if localizacao and len(localizacao) > 255:
        raise HTTPException(status_code=422, detail="Localizacao muito longa (max. 255 caracteres)")

    nome_anterior = maquina.nome_local
    localizacao_anterior = maquina.localizacao
    maquina.nome_local = nome
    maquina.localizacao = localizacao
    registrar_auditoria(
        db,
        user,
        acao="MAQUINA_PONTO_ATUALIZADO",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=f"Ponto atualizado nome={nome_anterior}->{nome} localizacao={localizacao_anterior}->{localizacao}",
    )
    db.commit()
    return {"id_hardware": maquina.id_hardware, "nome": maquina.nome_local, "localizacao": maquina.localizacao}

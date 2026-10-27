"""Horario em que o ponto da maquina fica fechado (ver
app/services/horario_fechado.py). Admin ou o cliente dono da maquina pode
configurar."""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.escopo import obter_maquina_do_usuario
from app.db.session import SessionLocal
from app.services.auditoria import registrar_auditoria
from app.services.horario_fechado import carregar_horarios, ponto_fechado_agora, salvar_horarios

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class HorarioFechadoItem(BaseModel):
    dia: int
    inicio: str
    fim: str


class HorarioFechadoIn(BaseModel):
    horarios: List[HorarioFechadoItem] = []


def _resposta(maquina) -> dict:
    return {
        "maquina_id": maquina.id_hardware,
        "horarios": carregar_horarios(maquina),
        "fechado_agora": ponto_fechado_agora(maquina),
    }


@router.get("/maquinas/{machine_id}/horario-fechado")
def obter_horario_fechado(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _resposta(obter_maquina_do_usuario(db, user, machine_id))


@router.put("/maquinas/{machine_id}/horario-fechado")
def salvar_horario_fechado(
    machine_id: str,
    payload: HorarioFechadoIn,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    maquina = obter_maquina_do_usuario(db, user, machine_id)
    try:
        horarios = salvar_horarios(maquina, [item.model_dump() for item in payload.horarios])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    descricao = ", ".join(f"{h['dia']}:{h['inicio']}-{h['fim']}" for h in horarios) or "nunca fecha"
    registrar_auditoria(
        db,
        user,
        acao="MAQUINA_HORARIO_FECHADO",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=f"Horario fechado: {descricao}",
    )
    db.commit()
    db.refresh(maquina)
    return _resposta(maquina)

"""Horario em que o ponto da maquina fica fechado e fechamentos pontuais
(feriado, reforma) - ver app/services/horario_fechado.py. Admin ou o cliente
dono da maquina pode configurar."""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.escopo import filtrar_maquinas_do_usuario, obter_maquina_do_usuario
from app.db.session import SessionLocal
from app.models.models import Maquina
from app.services.auditoria import registrar_auditoria
from app.services.horario_fechado import (
    carregar_horarios,
    carregar_pontuais,
    ponto_fechado_agora,
    salvar_horarios,
    salvar_pontuais,
)

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


class FechamentoPontualItem(BaseModel):
    inicio: str
    fim: str
    motivo: Optional[str] = None


class HorarioFechadoIn(BaseModel):
    horarios: List[HorarioFechadoItem] = []
    # None = nao mexe nos fechamentos pontuais.
    pontuais: Optional[List[FechamentoPontualItem]] = None
    # Copia o mesmo horario/fechamentos para as outras maquinas do mesmo cliente.
    aplicar_todas_do_cliente: bool = False


def _resposta(maquina, aplicadas: int = 1) -> dict:
    return {
        "maquina_id": maquina.id_hardware,
        "horarios": carregar_horarios(maquina),
        "pontuais": carregar_pontuais(maquina),
        "fechado_agora": ponto_fechado_agora(maquina),
        "maquinas_atualizadas": aplicadas,
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
    alvos = [maquina]
    if payload.aplicar_todas_do_cliente and maquina.cliente_id is not None:
        alvos = (
            filtrar_maquinas_do_usuario(db.query(Maquina), user)
            .filter(Maquina.cliente_id == maquina.cliente_id)
            .all()
        )
    try:
        for alvo in alvos:
            horarios = salvar_horarios(alvo, [item.model_dump() for item in payload.horarios])
            if payload.pontuais is not None:
                salvar_pontuais(alvo, [item.model_dump() for item in payload.pontuais])
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    descricao = ", ".join(f"{h['dia']}:{h['inicio']}-{h['fim']}" for h in horarios) or "nunca fecha"
    pontuais = carregar_pontuais(maquina)
    if pontuais:
        descricao += " | pontuais: " + ", ".join(f"{p['inicio']}~{p['fim']}" for p in pontuais)
    for alvo in alvos:
        registrar_auditoria(
            db,
            user,
            acao="MAQUINA_HORARIO_FECHADO",
            entidade_tipo="maquina",
            entidade_id=alvo.id_hardware,
            descricao=f"Horario fechado: {descricao}",
        )
    db.commit()
    db.refresh(maquina)
    return _resposta(maquina, len(alvos))

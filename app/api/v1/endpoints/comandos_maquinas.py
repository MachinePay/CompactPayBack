from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import ComandoMaquina, Maquina

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _serialize_command(command: ComandoMaquina, machine_name: str | None = None) -> dict:
    return {
        "id": command.id,
        "command_id": command.command_id,
        "maquina_id": command.maquina_id,
        "nome_local": machine_name,
        "tipo": command.tipo,
        "topic": command.topic,
        "payload": command.payload,
        "status": command.status,
        "detalhe_status": command.detalhe_status,
        "tentativas": command.tentativas,
        "max_tentativas": command.max_tentativas,
        "ultimo_erro": command.ultimo_erro,
        "next_retry_at": command.next_retry_at,
        "sent_at": command.sent_at,
        "ack_at": command.ack_at,
        "finished_at": command.finished_at,
        "created_at": command.created_at,
        "updated_at": command.updated_at,
    }


@router.get("/comandos-maquinas")
def listar_comandos_maquinas(
    machine_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    tipo: str | None = Query(default=None),
    limite: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    query = (
        db.query(ComandoMaquina, Maquina.nome_local)
        .join(Maquina, Maquina.id_hardware == ComandoMaquina.maquina_id)
    )
    if role != "admin":
        query = query.filter(Maquina.cliente_id == cliente_id)
    if machine_id:
        query = query.filter(ComandoMaquina.maquina_id == machine_id)
    if status:
        query = query.filter(ComandoMaquina.status == status)
    if tipo:
        query = query.filter(ComandoMaquina.tipo == tipo)

    rows = query.order_by(ComandoMaquina.created_at.desc()).limit(limite).all()
    return {
        "items": [_serialize_command(command, nome_local) for command, nome_local in rows],
        "count": len(rows),
    }

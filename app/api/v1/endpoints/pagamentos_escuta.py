from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.escopo import eh_admin, maquinas_do_usuario, obter_maquina_do_usuario
from app.db.session import SessionLocal
from app.models.models import EscutaTerminal, Maquina

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_maquina_visivel(db: Session, maquina_id: str, role: str, cliente_id):
    # Regra central de acesso (app/core/escopo.py): cliente so as proprias.
    return obter_maquina_do_usuario(db, (None, role, cliente_id), maquina_id)


@router.post("/pagamentos/escuta/iniciar")
def iniciar_escuta_terminal(
    dados: dict,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    machine_id = (dados.get("maquina_id") or "").strip()
    terminal_id = (dados.get("terminal_id") or "").strip()
    if not machine_id:
        raise HTTPException(status_code=422, detail="maquina_id e obrigatorio")
    if not terminal_id:
        raise HTTPException(status_code=422, detail="terminal_id e obrigatorio")

    _get_maquina_visivel(db, machine_id, role, cliente_id)
    escuta = db.query(EscutaTerminal).filter(EscutaTerminal.terminal_id == terminal_id).first()
    now = datetime.utcnow()
    if escuta:
        escuta.maquina_id = machine_id
        escuta.ativo = True
        escuta.updated_at = now
    else:
        escuta = EscutaTerminal(
            terminal_id=terminal_id,
            maquina_id=machine_id,
            ativo=True,
            created_at=now,
            updated_at=now,
        )
        db.add(escuta)
    db.commit()
    return {
        "ok": True,
        "terminal_id": terminal_id,
        "machine_id": machine_id,
        "mensagem": "Escuta ativada. Aguardando pagamentos aprovados da maquininha.",
    }


@router.post("/pagamentos/escuta/parar")
def parar_escuta_terminal(dados: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    terminal_id = (dados.get("terminal_id") or "").strip()
    if not terminal_id:
        raise HTTPException(status_code=422, detail="terminal_id e obrigatorio")
    escuta = db.query(EscutaTerminal).filter(EscutaTerminal.terminal_id == terminal_id).first()
    if escuta and escuta.maquina_id:
        # So para escuta de maquina que o login pode ver (regra de escopo.py).
        obter_maquina_do_usuario(db, user, escuta.maquina_id)
    elif escuta and not eh_admin(user):
        raise HTTPException(status_code=404, detail="Escuta nao encontrada")
    ativo_antes = bool(escuta and escuta.ativo)
    if escuta:
        escuta.ativo = False
        escuta.updated_at = datetime.utcnow()
        db.commit()
    return {"ok": True, "terminal_id": terminal_id, "ativo_antes": ativo_antes}


@router.get("/pagamentos/escuta")
def listar_escutas(db: Session = Depends(get_db), user=Depends(get_current_user)):
    escutas = db.query(EscutaTerminal).filter(EscutaTerminal.ativo.is_(True)).all()
    if not eh_admin(user):
        visiveis = {m.id_hardware for m in maquinas_do_usuario(db, user)}
        escutas = [item for item in escutas if item.maquina_id in visiveis]
    return {
        "ativos": [
            {
                "terminal_id": item.terminal_id,
                "machine_id": item.maquina_id,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in escutas
        ]
    }

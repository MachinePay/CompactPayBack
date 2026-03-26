from datetime import date, datetime, timedelta
import secrets
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.v1.endpoints import auth, produtos, usuarios
from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import Maquina, Transacao
from app.schemas.maquina import MaquinaCreate, MaquinaOut
from app.services.mqtt_commands import publish_machine_credit

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _maquina_query_por_usuario(db: Session, role: str, cliente_id):
    if role == "admin":
        return db.query(Maquina)
    return db.query(Maquina).filter(Maquina.cliente_id == cliente_id)


def _generate_machine_id(db: Session) -> str:
    while True:
        candidate = f"CPM-{secrets.token_hex(3).upper()}"
        exists = db.query(Maquina).filter(Maquina.id_hardware == candidate).first()
        if not exists:
            return candidate


@router.get("/maquinas", response_model=List[MaquinaOut])
def listar_maquinas(db: Session = Depends(get_db), user=Depends(get_current_user)):
    _, role, cliente_id = user
    maquinas = _maquina_query_por_usuario(db, role, cliente_id).all()
    agora = datetime.utcnow()
    resultado = []

    for maquina in maquinas:
        faturamento = (
            db.query(func.sum(Transacao.valor))
            .filter(
                Transacao.maquina_id == maquina.id_hardware,
                Transacao.tipo == "IN",
            )
            .scalar()
            or 0.0
        )
        resultado.append(
            {
                "id_hardware": maquina.id_hardware,
                "cliente_id": maquina.cliente_id,
                "nome": maquina.nome_local,
                "localizacao": None,
                "ultimo_sinal": maquina.ultimo_sinal,
                "status_online": bool(
                    maquina.ultimo_sinal
                    and (agora - maquina.ultimo_sinal) < timedelta(minutes=3)
                ),
                "faturamento": float(faturamento),
            }
        )

    return resultado


@router.get("/maquinas/novo-id")
def gerar_novo_id_maquina(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode gerar ids de maquinas")
    return {"id_hardware": _generate_machine_id(db)}


@router.post("/maquinas", response_model=MaquinaOut)
def criar_maquina(
    maquina: MaquinaCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode criar maquinas")
    machine_id = maquina.id_hardware or _generate_machine_id(db)
    if db.query(Maquina).filter(Maquina.id_hardware == machine_id).first():
        raise HTTPException(status_code=400, detail="Maquina ja cadastrada")

    db_maquina = Maquina(
        id_hardware=machine_id,
        cliente_id=maquina.cliente_id,
        nome_local=maquina.nome,
        ultimo_sinal=datetime.utcnow(),
    )
    db.add(db_maquina)
    db.commit()
    db.refresh(db_maquina)

    return {
        "id_hardware": db_maquina.id_hardware,
        "cliente_id": db_maquina.cliente_id,
        "nome": db_maquina.nome_local,
        "localizacao": maquina.localizacao,
        "ultimo_sinal": db_maquina.ultimo_sinal,
        "status_online": True,
        "faturamento": 0.0,
    }


@router.post("/maquinas/{machine_id}/credito-teste")
def enviar_credito_teste(
    machine_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    query = _maquina_query_por_usuario(db, role, cliente_id)
    maquina = query.filter(Maquina.id_hardware == machine_id).first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")

    try:
        payload = publish_machine_credit(machine_id, action="paid")
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Falha ao enviar comando MQTT para a maquina") from exc

    return {
        "ok": True,
        "machine_id": machine_id,
        "topic": f"/TEF/{machine_id}/cmd",
        "payload": payload,
    }


@router.get("/faturamento")
def faturamento(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    id_hardware: str = None,
    periodo: str = "dia",
    data_inicio: str = None,
    data_fim: str = None,
):
    _, role, cliente_id = user
    query = db.query(Transacao)
    if role != "admin":
        maquinas_ids = [
            m.id_hardware for m in _maquina_query_por_usuario(db, role, cliente_id).all()
        ]
        query = query.filter(Transacao.maquina_id.in_(maquinas_ids))
    if id_hardware:
        query = query.filter(Transacao.maquina_id == id_hardware)
    if data_inicio and data_fim:
        dt_inicio = datetime.fromisoformat(data_inicio)
        dt_fim = datetime.fromisoformat(data_fim)
        query = query.filter(
            Transacao.data_hora >= dt_inicio,
            Transacao.data_hora <= dt_fim,
        )
    elif periodo == "dia":
        hoje = date.today()
        query = query.filter(func.date(Transacao.data_hora) == hoje)
    elif periodo == "mes":
        hoje = date.today()
        query = query.filter(func.extract("month", Transacao.data_hora) == hoje.month)
        query = query.filter(func.extract("year", Transacao.data_hora) == hoje.year)

    total = query.with_entities(func.sum(Transacao.valor)).scalar() or 0.0
    return {"faturamento": float(total)}


router.include_router(auth.router)
router.include_router(usuarios.router)
router.include_router(produtos.router)


@router.get("/dashboard/stats")
def dashboard_stats(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    hoje = date.today()
    _, role, cliente_id = user
    query = db.query(Transacao)
    if role != "admin":
        maquinas_ids = [
            m.id_hardware for m in _maquina_query_por_usuario(db, role, cliente_id).all()
        ]
        query = query.filter(Transacao.maquina_id.in_(maquinas_ids))

    faturamento = (
        query.with_entities(func.sum(Transacao.valor))
        .filter(
            Transacao.tipo == "IN",
            func.date(Transacao.data_hora) == hoje,
        )
        .scalar()
        or 0.0
    )
    premios = (
        query.with_entities(func.count(Transacao.id))
        .filter(
            Transacao.tipo == "OUT",
            func.date(Transacao.data_hora) == hoje,
        )
        .scalar()
        or 0
    )
    return {
        "faturamento_total_dia": float(faturamento),
        "premios_entregues": int(premios),
    }

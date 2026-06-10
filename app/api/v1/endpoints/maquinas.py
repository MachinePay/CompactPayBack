from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import AuditoriaOperacao, HistoricoOperacao, Maquina
from app.services.auditoria import registrar_auditoria
from app.services.mqtt_commands import publish_machine_credit

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _generate_machine_id(db: Session) -> str:
    numeric_ids = []
    for (id_hardware,) in db.query(Maquina.id_hardware).all():
        value = str(id_hardware or "").strip()
        if value.isdigit():
            numeric_ids.append(int(value))

    next_id = max(numeric_ids, default=999) + 1
    next_id = max(next_id, 1000)
    while db.query(Maquina).filter(Maquina.id_hardware == str(next_id)).first():
        next_id += 1
    return str(next_id)


def _get_maquina_visivel(db: Session, machine_id: str, role: str, cliente_id):
    query = db.query(Maquina)
    if role != "admin":
        query = query.filter(Maquina.cliente_id == cliente_id)
    maquina = query.filter(Maquina.id_hardware == machine_id).first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")
    return maquina


def _get_user_email(user) -> str:
    token_data, _, _ = user
    return token_data.email


@router.get("/maquinas/novo-id")
def gerar_novo_id_maquina(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode gerar ids de maquinas")
    return {"id_hardware": _generate_machine_id(db)}


@router.post("/maquinas/{machine_id}/credito-teste")
def enviar_credito_teste(
    machine_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    _get_maquina_visivel(db, machine_id, role, cliente_id)

    try:
        payload = publish_machine_credit(machine_id, action="paid")
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Falha ao enviar comando MQTT para a maquina") from exc

    db.add(
        HistoricoOperacao(
            maquina_id=machine_id,
            categoria="TESTE",
            descricao="Credito de teste enviado pelo painel",
            valor=None,
            created_at=datetime.utcnow(),
        )
    )
    db.add(
        AuditoriaOperacao(
            maquina_id=machine_id,
            acao="TESTE_CREDITO",
            descricao="Credito de teste enviado pelo painel",
            executado_por_email=_get_user_email(user),
            created_at=datetime.utcnow(),
        )
    )
    registrar_auditoria(
        db,
        user,
        acao="TESTE_CREDITO",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=f"Credito de teste enviado pelo painel payload={payload}",
    )
    db.commit()

    return {
        "ok": True,
        "machine_id": machine_id,
        "topic": f"/TEF/{machine_id}/cmd",
        "payload": payload,
    }


@router.post("/maquinas/{machine_id}/observacoes")
def registrar_observacao_maquina(
    machine_id: str,
    payload: dict,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    _get_maquina_visivel(db, machine_id, role, cliente_id)

    descricao = (payload.get("descricao") or "").strip()
    if not descricao:
        raise HTTPException(status_code=400, detail="Descricao da observacao e obrigatoria")

    historico = HistoricoOperacao(
        maquina_id=machine_id,
        categoria="MANUTENCAO",
        descricao=descricao,
        valor=None,
        created_at=datetime.utcnow(),
    )
    db.add(historico)
    db.add(
        AuditoriaOperacao(
            maquina_id=machine_id,
            acao="OBSERVACAO_REGISTRADA",
            descricao=descricao,
            executado_por_email=_get_user_email(user),
            created_at=datetime.utcnow(),
        )
    )
    registrar_auditoria(
        db,
        user,
        acao="OBSERVACAO_REGISTRADA",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=f"Observacao registrada: {descricao}",
    )
    db.commit()
    db.refresh(historico)
    return {
        "id": historico.id,
        "maquina_id": historico.maquina_id,
        "categoria": historico.categoria,
        "descricao": historico.descricao,
        "valor": historico.valor,
        "created_at": historico.created_at,
    }

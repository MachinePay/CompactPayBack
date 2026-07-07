from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import AuditoriaSistema

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _resolve_periodo_opcional(periodo: str | None, data_inicio: str | None, data_fim: str | None):
    if data_inicio and data_fim:
        return (
            datetime.fromisoformat(data_inicio),
            datetime.fromisoformat(data_fim) + timedelta(days=1) - timedelta(microseconds=1),
        )
    hoje = date.today()
    if periodo == "hoje":
        return datetime.combine(hoje, datetime.min.time()), datetime.combine(hoje, datetime.max.time())
    if periodo == "semana":
        end = datetime.combine(hoje, datetime.max.time())
        return end - timedelta(days=6), end
    if periodo == "mes":
        return datetime.combine(hoje.replace(day=1), datetime.min.time()), datetime.combine(hoje, datetime.max.time())
    return None, None


@router.get("/auditoria-sistema")
def listar_auditoria_sistema(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    entidade_tipo: str = None,
    entidade_id: str = None,
    maquina_id: str = None,
    usuario: str = None,
    acao: str = None,
    periodo: str = None,
    data_inicio: str = None,
    data_fim: str = None,
    limite: int = 100,
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode consultar auditoria do sistema")

    query = db.query(AuditoriaSistema)
    if entidade_tipo:
        query = query.filter(AuditoriaSistema.entidade_tipo == entidade_tipo)
    if entidade_id:
        query = query.filter(AuditoriaSistema.entidade_id == entidade_id)
    if maquina_id:
        query = query.filter(
            AuditoriaSistema.entidade_tipo == "maquina",
            AuditoriaSistema.entidade_id == maquina_id,
        )
    if usuario:
        query = query.filter(AuditoriaSistema.executado_por_email.ilike(f"%{usuario}%"))
    if acao:
        query = query.filter(AuditoriaSistema.acao == acao)

    inicio, fim = _resolve_periodo_opcional(periodo, data_inicio, data_fim)
    if inicio and fim:
        query = query.filter(AuditoriaSistema.created_at >= inicio, AuditoriaSistema.created_at <= fim)

    items = query.order_by(AuditoriaSistema.created_at.desc()).limit(min(max(limite, 1), 500)).all()
    return [
        {
            "id": item.id,
            "entidade_tipo": item.entidade_tipo,
            "entidade_id": item.entidade_id,
            "acao": item.acao,
            "descricao": item.descricao,
            "executado_por_email": item.executado_por_email,
            "created_at": item.created_at,
        }
        for item in items
    ]

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import AuditoriaSistema

router = APIRouter()

# Offset fixo UTC-3: o Brasil aboliu o horario de verao em 2019.
BRASILIA_TZ = timezone(timedelta(hours=-3))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _brasilia_local_to_utc_naive(value: datetime) -> datetime:
    return value.replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc).replace(tzinfo=None)


def _resolve_periodo_opcional(periodo: str | None, data_inicio: str | None, data_fim: str | None):
    # created_at e' gravado em UTC (datetime.utcnow()), mas periodo/data_inicio/
    # data_fim sao pensados no calendario de Brasilia - por isso a janela e'
    # montada em horario local e so convertida pra UTC no final (senao um
    # registro feito a noite em Brasilia some do filtro "hoje").
    if data_inicio and data_fim:
        start_local = datetime.fromisoformat(data_inicio)
        end_local = datetime.fromisoformat(data_fim) + timedelta(days=1) - timedelta(microseconds=1)
        return (
            _brasilia_local_to_utc_naive(start_local),
            _brasilia_local_to_utc_naive(end_local),
        )
    hoje = datetime.now(BRASILIA_TZ).date()
    if periodo == "hoje":
        start_local = datetime.combine(hoje, datetime.min.time())
        end_local = datetime.combine(hoje, datetime.max.time())
        return _brasilia_local_to_utc_naive(start_local), _brasilia_local_to_utc_naive(end_local)
    if periodo == "semana":
        end_local = datetime.combine(hoje, datetime.max.time())
        start_local = end_local - timedelta(days=6)
        return _brasilia_local_to_utc_naive(start_local), _brasilia_local_to_utc_naive(end_local)
    if periodo == "mes":
        start_local = datetime.combine(hoje.replace(day=1), datetime.min.time())
        end_local = datetime.combine(hoje, datetime.max.time())
        return _brasilia_local_to_utc_naive(start_local), _brasilia_local_to_utc_naive(end_local)
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

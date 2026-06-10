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


@router.get("/auditoria-sistema")
def listar_auditoria_sistema(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    entidade_tipo: str = None,
    entidade_id: str = None,
    acao: str = None,
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
    if acao:
        query = query.filter(AuditoriaSistema.acao == acao)

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

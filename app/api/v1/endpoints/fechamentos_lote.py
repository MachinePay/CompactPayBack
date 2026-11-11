"""Resumo para fechamento/desfazer fechamento de varias maquinas de uma vez
(tela de Maquinas). So leitura: quem fecha/desfaz de fato sao os endpoints
de sempre (POST/DELETE /maquinas/{id}/fechamentos), chamados um por maquina
pelo frontend - assim a regra do fechamento continua num lugar so."""

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.escopo import maquinas_do_usuario
from app.db.session import SessionLocal
from app.models.models import FechamentoMaquina
from app.schemas.fechamento import FechamentoMaquinaOut
from app.services.maquinas_relatorio import serialize_machines_summary_batch

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ultimos_fechamentos(db: Session, machine_ids: list[str]) -> dict[str, FechamentoMaquina]:
    if not machine_ids:
        return {}
    ultimo_id_por_maquina = (
        db.query(FechamentoMaquina.maquina_id, func.max(FechamentoMaquina.id).label("ultimo_id"))
        .filter(FechamentoMaquina.maquina_id.in_(machine_ids))
        .group_by(FechamentoMaquina.maquina_id)
        .subquery()
    )
    fechamentos = (
        db.query(FechamentoMaquina)
        .join(ultimo_id_por_maquina, FechamentoMaquina.id == ultimo_id_por_maquina.c.ultimo_id)
        .all()
    )
    return {item.maquina_id: item for item in fechamentos}


@router.get("/fechamentos-lote/maquinas")
def resumo_fechamento_lote(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    cliente_id: int = None,
):
    maquinas = maquinas_do_usuario(db, user, cliente_id)
    maquinas.sort(key=lambda item: ((item.nome_local or "").lower(), item.id_hardware))
    # periodo="fechamento": o resumo ja corta pelo ultimo fechamento de cada
    # maquina, entao "faturamento" aqui e' exatamente o que esta em aberto.
    resumos = serialize_machines_summary_batch(db, maquinas, periodo="fechamento")
    resumo_por_id = {item["id_hardware"]: item for item in resumos}
    ultimos = _ultimos_fechamentos(db, [item.id_hardware for item in maquinas])

    resultado = []
    for maquina in maquinas:
        resumo = resumo_por_id.get(maquina.id_hardware, {})
        ultimo = ultimos.get(maquina.id_hardware)
        resultado.append(
            {
                "id_hardware": maquina.id_hardware,
                "nome": maquina.nome_local,
                "localizacao": maquina.localizacao,
                "status_online": bool(resumo.get("status_online")),
                "aberto_total": float(resumo.get("faturamento") or 0.0),
                "aberto_fisico": float(resumo.get("faturamento_fisico") or 0.0),
                "ultima_atividade_em": resumo.get("ultima_atividade_em"),
                "ultimo_fechamento": (
                    FechamentoMaquinaOut.model_validate(ultimo).model_dump() if ultimo else None
                ),
            }
        )
    return resultado

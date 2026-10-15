"""REGRA DE ACESSO DAS MAQUINAS (vale para TODA rota que mostra dado de maquina):

- admin: ve e mexe em todas as maquinas.
- cliente: ve SOMENTE as maquinas do proprio cliente (Maquina.cliente_id ==
  cliente_id do login). Nunca as de outro cliente, nem maquina sem dono.
- Um login de cliente sem cliente vinculado e' recusado ja em
  get_current_user (app/core/dependencies.py), entao o filtro abaixo nunca
  compara com None (no SQLAlchemy, "coluna == None" vira IS NULL e mostraria
  as maquinas sem dono).

Rotas novas devem usar estas funcoes em vez de montar o filtro na mao.
Para maquina de outro cliente a resposta e' 404 (nao revela que ela existe).
"""
from fastapi import HTTPException
from sqlalchemy import false
from sqlalchemy.orm import Query, Session, joinedload

from app.models.models import Maquina


def eh_admin(user) -> bool:
    return user[1] == "admin"


def filtrar_maquinas_do_usuario(query: Query, user) -> Query:
    """Aplica a regra de acesso numa query que tem Maquina."""
    if eh_admin(user):
        return query
    cliente_id = user[2]
    if cliente_id is None:  # defesa extra: get_current_user ja recusa esse login
        return query.filter(false())
    return query.filter(Maquina.cliente_id == cliente_id)


def maquinas_do_usuario(db: Session, user, cliente_id_filtro: int | None = None) -> list[Maquina]:
    """Maquinas que o login pode ver. cliente_id_filtro so vale para admin
    (cliente nunca escolhe outro cliente: o filtro e' ignorado)."""
    query = filtrar_maquinas_do_usuario(db.query(Maquina).options(joinedload(Maquina.dono)), user)
    if eh_admin(user) and cliente_id_filtro is not None:
        query = query.filter(Maquina.cliente_id == cliente_id_filtro)
    return query.all()


def obter_maquina_do_usuario(db: Session, user, machine_id: str) -> Maquina:
    maquina = filtrar_maquinas_do_usuario(db.query(Maquina), user).filter(Maquina.id_hardware == machine_id).first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")
    return maquina

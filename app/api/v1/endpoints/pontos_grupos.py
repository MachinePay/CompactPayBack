"""Pontos (grupos opcionais de maquinas dentro de um cliente).

So organiza a visualizacao da lista de maquinas: nao mexe em pagamento,
contagem nem no nome_local da maquina. O cliente cria/renomeia/exclui os
proprios pontos e poe/tira as proprias maquinas; o admin mexe em todos.
Ponto ou maquina de outro cliente da 404 (regra de app/core/escopo.py).
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.escopo import eh_admin, filtrar_maquinas_do_usuario, obter_maquina_do_usuario
from app.db.session import SessionLocal
from app.models.models import Cliente, Maquina
from app.models.pontos import PontoGrupo
from app.services.auditoria import registrar_auditoria

router = APIRouter()

NOME_MAX = 80


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class PontoCriar(BaseModel):
    nome: str
    cliente_id: Optional[int] = None  # so admin usa; cliente usa o proprio


class PontoRenomear(BaseModel):
    nome: str


class MaquinaPonto(BaseModel):
    ponto_id: Optional[int] = None  # None tira a maquina do ponto


def _nome_valido(nome: str) -> str:
    nome = (nome or "").strip()
    if not nome:
        raise HTTPException(status_code=422, detail="Informe o nome do ponto")
    if len(nome) > NOME_MAX:
        raise HTTPException(status_code=422, detail=f"Nome do ponto muito longo (max. {NOME_MAX} caracteres)")
    return nome


def _filtrar_pontos_do_usuario(query, user):
    if eh_admin(user):
        return query
    return query.filter(PontoGrupo.cliente_id == user[2])


def _obter_ponto(db: Session, user, ponto_id: int) -> PontoGrupo:
    ponto = _filtrar_pontos_do_usuario(db.query(PontoGrupo), user).filter(PontoGrupo.id == ponto_id).first()
    if not ponto:
        raise HTTPException(status_code=404, detail="Ponto nao encontrado")
    return ponto


def _checar_nome_repetido(db: Session, cliente_id: int, nome: str, ignorar_id: int | None = None):
    query = db.query(PontoGrupo).filter(
        PontoGrupo.cliente_id == cliente_id,
        func.lower(PontoGrupo.nome) == nome.lower(),
    )
    if ignorar_id is not None:
        query = query.filter(PontoGrupo.id != ignorar_id)
    if query.first():
        raise HTTPException(status_code=409, detail="Ja existe um ponto com esse nome")


def _ponto_out(ponto: PontoGrupo) -> dict:
    return {"id": ponto.id, "cliente_id": ponto.cliente_id, "nome": ponto.nome}


@router.get("/pontos")
def listar_pontos(
    cliente_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Pontos do usuario e em qual ponto cada maquina esta.
    cliente_id so vale para admin (cliente sempre ve so os proprios)."""
    pontos_q = _filtrar_pontos_do_usuario(db.query(PontoGrupo), user)
    maquinas_q = filtrar_maquinas_do_usuario(
        db.query(Maquina.id_hardware, Maquina.cliente_id, Maquina.ponto_id), user
    ).filter(Maquina.ponto_id.isnot(None))
    if eh_admin(user) and cliente_id is not None:
        pontos_q = pontos_q.filter(PontoGrupo.cliente_id == cliente_id)
        maquinas_q = maquinas_q.filter(Maquina.cliente_id == cliente_id)
    pontos = pontos_q.order_by(func.lower(PontoGrupo.nome), PontoGrupo.id).all()
    dono_do_ponto = {p.id: p.cliente_id for p in pontos}
    # Maquina que mudou de cliente pode apontar para ponto de outro dono: ignora.
    maquinas = {
        id_hardware: ponto_id
        for id_hardware, maq_cliente_id, ponto_id in maquinas_q.all()
        if dono_do_ponto.get(ponto_id) == maq_cliente_id
    }
    return {"pontos": [_ponto_out(p) for p in pontos], "maquinas": maquinas}


@router.post("/pontos")
def criar_ponto(dados: PontoCriar, db: Session = Depends(get_db), user=Depends(get_current_user)):
    nome = _nome_valido(dados.nome)
    if eh_admin(user):
        if dados.cliente_id is None:
            raise HTTPException(status_code=422, detail="Escolha o cliente do ponto")
        if not db.query(Cliente.id).filter(Cliente.id == dados.cliente_id).first():
            raise HTTPException(status_code=404, detail="Cliente nao encontrado")
        cliente_id = dados.cliente_id
    else:
        cliente_id = user[2]
    _checar_nome_repetido(db, cliente_id, nome)
    ponto = PontoGrupo(cliente_id=cliente_id, nome=nome)
    db.add(ponto)
    db.flush()
    registrar_auditoria(
        db, user, acao="PONTO_GRUPO_CRIADO", entidade_tipo="ponto_grupo",
        entidade_id=str(ponto.id), descricao=f"Ponto criado nome={nome} cliente_id={cliente_id}",
    )
    db.commit()
    return _ponto_out(ponto)


@router.patch("/pontos/{ponto_id}")
def renomear_ponto(
    ponto_id: int, dados: PontoRenomear, db: Session = Depends(get_db), user=Depends(get_current_user)
):
    ponto = _obter_ponto(db, user, ponto_id)
    nome = _nome_valido(dados.nome)
    _checar_nome_repetido(db, ponto.cliente_id, nome, ignorar_id=ponto.id)
    anterior = ponto.nome
    ponto.nome = nome
    registrar_auditoria(
        db, user, acao="PONTO_GRUPO_RENOMEADO", entidade_tipo="ponto_grupo",
        entidade_id=str(ponto.id), descricao=f"Ponto renomeado {anterior}->{nome}",
    )
    db.commit()
    return _ponto_out(ponto)


@router.delete("/pontos/{ponto_id}")
def excluir_ponto(ponto_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Exclui o ponto; as maquinas dele voltam para "Sem ponto"."""
    ponto = _obter_ponto(db, user, ponto_id)
    soltas = (
        db.query(Maquina)
        .filter(Maquina.ponto_id == ponto.id)
        .update({Maquina.ponto_id: None}, synchronize_session=False)
    )
    registrar_auditoria(
        db, user, acao="PONTO_GRUPO_EXCLUIDO", entidade_tipo="ponto_grupo",
        entidade_id=str(ponto.id), descricao=f"Ponto excluido nome={ponto.nome} maquinas_soltas={soltas}",
    )
    db.delete(ponto)
    db.commit()
    return {"ok": True, "maquinas_soltas": soltas}


@router.put("/pontos/maquinas/{machine_id}")
def definir_ponto_da_maquina(
    machine_id: str, dados: MaquinaPonto, db: Session = Depends(get_db), user=Depends(get_current_user)
):
    """Poe a maquina num ponto (do mesmo cliente dela) ou tira (ponto_id null)."""
    maquina = obter_maquina_do_usuario(db, user, machine_id)
    if dados.ponto_id is not None:
        ponto = _obter_ponto(db, user, dados.ponto_id)
        if ponto.cliente_id != maquina.cliente_id:
            raise HTTPException(status_code=422, detail="O ponto e a maquina sao de clientes diferentes")
    anterior = maquina.ponto_id
    maquina.ponto_id = dados.ponto_id
    registrar_auditoria(
        db, user, acao="MAQUINA_PONTO_GRUPO", entidade_tipo="maquina",
        entidade_id=machine_id, descricao=f"Ponto (grupo) {anterior}->{dados.ponto_id}",
    )
    db.commit()
    return {"id_hardware": maquina.id_hardware, "ponto_id": maquina.ponto_id}

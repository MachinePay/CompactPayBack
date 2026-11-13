"""Rotas do banco "mercado_pago_point" (tela touch ESP32 + Point em modo integrado).

- /tela/point/... e /tela/telas...: painel (login JWT) ve as Points da conta,
  coloca uma Point em modo integrado e cadastra/revoga a tela de cada maquina.
- /tela/config e /tela/cobrancas...: a propria tela, header X-Tela-Key.

Logica em app/services/tela_point.py. Nada aqui altera rotas/fluxos existentes;
o pagamento aprovado segue pelo webhook de order que ja existe.
"""
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.escopo import eh_admin, obter_maquina_do_usuario
from app.db.session import SessionLocal
from app.models.models import Cliente
from app.models.produto import Produto
from app.models.tela_point import TelaPoint
from app.services import tela_point as svc
from app.services.auditoria import registrar_auditoria
from app.services.smartapp import maquina_online, verificar_placa_online

router = APIRouter(prefix="/tela", tags=["tela-point"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_tela(
    x_tela_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> TelaPoint:
    return svc.autenticar_tela(db, x_tela_key)


class TelaCreate(BaseModel):
    maquina_id: str = Field(min_length=1)
    nome: str = Field(min_length=1, max_length=80)
    point_terminal_id: str = Field(min_length=3, max_length=120)


class CobrancaCreate(BaseModel):
    produto_id: int


def _serialize_tela(tela: TelaPoint) -> dict:
    return {
        "id": tela.id,
        "cliente_id": tela.cliente_id,
        "maquina_id": tela.maquina_id,
        "nome": tela.nome,
        "point_terminal_id": tela.point_terminal_id,
        "chave_prefixo": tela.chave_prefixo,
        "ativo": tela.ativo,
        "created_at": tela.created_at,
        "ultimo_uso_em": tela.ultimo_uso_em,
    }


def _cliente_do_painel(db: Session, user, cliente_id: int | None) -> int:
    cliente_id = cliente_id if eh_admin(user) else user[2]
    if cliente_id is None:
        raise HTTPException(status_code=422, detail="Informe o cliente")
    if not db.query(Cliente).filter(Cliente.id == cliente_id).first():
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    return cliente_id


# ---- Painel: Points da conta ----------------------------------------------

@router.get("/point/terminais")
def listar_points(cliente_id: int | None = None, db: Session = Depends(get_db), user=Depends(get_current_user)):
    cliente_id = _cliente_do_painel(db, user, cliente_id)
    return svc.listar_terminais_point(svc.token_do_cliente(db, cliente_id))


@router.post("/point/terminais/{terminal_id}/modo-integrado")
def ativar_modo_integrado(
    terminal_id: str,
    cliente_id: int | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    cliente_id = _cliente_do_painel(db, user, cliente_id)
    resultado = svc.ativar_modo_pdv(svc.token_do_cliente(db, cliente_id), terminal_id)
    registrar_auditoria(
        db,
        user,
        acao="POINT_MODO_INTEGRADO",
        entidade_tipo="point_terminal",
        entidade_id=terminal_id,
        descricao=f"Point {terminal_id} colocada em modo integrado (PDV) cliente_id={cliente_id}",
    )
    db.commit()
    return {"ok": True, "terminal_id": terminal_id, "mercado_pago": resultado}


# ---- Painel: telas ---------------------------------------------------------

@router.post("/telas")
def criar_tela(dados: TelaCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    maquina = obter_maquina_do_usuario(db, user, dados.maquina_id.strip())
    if maquina.banco_pagamento != svc.BANCO_TELA_POINT:
        raise HTTPException(status_code=422, detail="A maquina precisa estar com o banco Mercado Pago Point (tela)")
    if not maquina.cliente_id:
        raise HTTPException(status_code=422, detail="Maquina sem cliente")
    point_terminal_id = dados.point_terminal_id.strip()
    svc.validar_point_livre(db, svc.token_do_cliente(db, maquina.cliente_id), maquina, point_terminal_id)

    # Uma tela ativa por maquina: cadastrar de novo troca a chave (tela perdida/trocada).
    anteriores = (
        db.query(TelaPoint)
        .filter(TelaPoint.maquina_id == maquina.id_hardware, TelaPoint.ativo.is_(True))
        .all()
    )
    for anterior in anteriores:
        anterior.ativo = False

    chave = svc.gerar_chave()
    tela = TelaPoint(
        cliente_id=maquina.cliente_id,
        maquina_id=maquina.id_hardware,
        nome=dados.nome.strip(),
        point_terminal_id=point_terminal_id,
        chave_prefixo=chave[:12],
        chave_hash=svc.hash_chave(chave),
        ativo=True,
    )
    db.add(tela)
    db.flush()
    registrar_auditoria(
        db,
        user,
        acao="TELA_POINT_CRIADA",
        entidade_tipo="tela_point",
        entidade_id=tela.id,
        descricao=(
            f"Tela Point criada maquina={maquina.id_hardware} point={tela.point_terminal_id} "
            f"nome={tela.nome} telas_revogadas={len(anteriores)}"
        ),
    )
    db.commit()
    db.refresh(tela)
    # A chave so aparece aqui; no banco fica so o hash.
    return {**_serialize_tela(tela), "chave": chave}


@router.get("/telas")
def listar_telas(db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = db.query(TelaPoint)
    if not eh_admin(user):
        query = query.filter(TelaPoint.cliente_id == user[2])
    return [_serialize_tela(t) for t in query.order_by(TelaPoint.id).all()]


@router.delete("/telas/{tela_id}")
def revogar_tela(tela_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = db.query(TelaPoint).filter(TelaPoint.id == tela_id)
    if not eh_admin(user):
        query = query.filter(TelaPoint.cliente_id == user[2])
    tela = query.first()
    if not tela:
        raise HTTPException(status_code=404, detail="Tela nao encontrada")
    tela.ativo = False
    registrar_auditoria(
        db,
        user,
        acao="TELA_POINT_REVOGADA",
        entidade_tipo="tela_point",
        entidade_id=tela.id,
        descricao=f"Tela Point revogada maquina={tela.maquina_id} nome={tela.nome}",
    )
    db.commit()
    return _serialize_tela(tela)


# ---- A propria tela --------------------------------------------------------

@router.get("/config")
def config_da_tela(db: Session = Depends(get_db), tela=Depends(get_tela)):
    maquina = svc.maquina_da_tela(db, tela)
    produtos = db.query(Produto).filter(Produto.maquina_id == maquina.id_hardware).order_by(Produto.valor).all()
    return {
        "maquina_id": maquina.id_hardware,
        "nome": maquina.nome_local or maquina.id_hardware,
        "online": maquina_online(maquina),
        "produtos": [{"id": p.id, "nome": p.nome, "valor": p.valor} for p in produtos if p.valor and p.valor > 0],
    }


@router.post("/cobrancas")
def criar_cobranca(dados: CobrancaCreate, db: Session = Depends(get_db), tela=Depends(get_tela)):
    maquina = svc.maquina_da_tela(db, tela)
    produto = svc.produto_da_maquina(db, maquina, dados.produto_id)
    # Nao cobra se a placa nao responder: o cliente pagaria e nao receberia o credito.
    if not verificar_placa_online(db, maquina):
        raise HTTPException(status_code=409, detail="Maquina indisponivel no momento")
    return svc.criar_cobranca(db, tela, maquina, produto)


@router.get("/cobrancas/{order_id}")
def consultar_cobranca(order_id: str, db: Session = Depends(get_db), tela=Depends(get_tela)):
    maquina = svc.maquina_da_tela(db, tela)
    return svc.consultar_cobranca(db, tela, maquina, order_id)


@router.post("/cobrancas/{order_id}/cancelar")
def cancelar_cobranca(order_id: str, db: Session = Depends(get_db), tela=Depends(get_tela)):
    maquina = svc.maquina_da_tela(db, tela)
    return svc.cancelar_cobranca(db, tela, maquina, order_id)

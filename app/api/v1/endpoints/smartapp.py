"""Rotas do banco "mercado_pago_apk" (app CompactPay na Point Smart).

- /smartapp/terminais...: painel (login JWT) cadastra e revoga a chave de cada Point.
- o resto: o proprio app, autenticado pelo header X-Terminal-Key.

Logica em app/services/smartapp.py. Nada aqui altera as rotas/fluxos existentes.
"""
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.escopo import eh_admin
from app.db.session import SessionLocal
from app.models.models import Cliente, ComandoMaquina, Maquina
from app.models.produto import Produto
from app.models.smartapp import TerminalSmartApp
from app.services import smartapp as svc
from app.services.auditoria import registrar_auditoria

router = APIRouter(prefix="/smartapp", tags=["smartapp"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_terminal(
    x_terminal_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> TerminalSmartApp:
    return svc.autenticar_terminal(db, x_terminal_key)


class TerminalCreate(BaseModel):
    nome: str = Field(min_length=1, max_length=80)
    cliente_id: int | None = None  # so admin escolhe; cliente usa o proprio


class PagamentoSmartApp(BaseModel):
    maquina_id: str
    produto_id: int | None = None
    valor: float = Field(gt=0)
    payment_id: str | int | None = None
    payment_reference: str = Field(min_length=1, max_length=120)
    metodo: str | None = None
    sn_terminal: str | None = None


def _serialize_terminal(terminal: TerminalSmartApp) -> dict:
    return {
        "id": terminal.id,
        "cliente_id": terminal.cliente_id,
        "nome": terminal.nome,
        "chave_prefixo": terminal.chave_prefixo,
        "ativo": terminal.ativo,
        "created_at": terminal.created_at,
        "ultimo_uso_em": terminal.ultimo_uso_em,
    }


# ---- Painel: chaves dos terminais ----------------------------------------

@router.post("/terminais")
def criar_terminal(dados: TerminalCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    cliente_id = dados.cliente_id if eh_admin(user) else user[2]
    if cliente_id is None:
        raise HTTPException(status_code=422, detail="Informe o cliente do terminal")
    if not db.query(Cliente).filter(Cliente.id == cliente_id).first():
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")

    chave = svc.gerar_chave()
    terminal = TerminalSmartApp(
        cliente_id=cliente_id,
        nome=dados.nome.strip(),
        chave_prefixo=chave[:10],
        chave_hash=svc.hash_chave(chave),
        ativo=True,
    )
    db.add(terminal)
    db.flush()
    registrar_auditoria(
        db,
        user,
        acao="SMARTAPP_TERMINAL_CRIADO",
        entidade_tipo="terminal_smartapp",
        entidade_id=terminal.id,
        descricao=f"Terminal SmartApp criado nome={terminal.nome} cliente_id={cliente_id}",
    )
    db.commit()
    db.refresh(terminal)
    # A chave so aparece aqui; no banco fica so o hash.
    return {**_serialize_terminal(terminal), "chave": chave}


@router.get("/terminais")
def listar_terminais(db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = db.query(TerminalSmartApp)
    if not eh_admin(user):
        query = query.filter(TerminalSmartApp.cliente_id == user[2])
    return [_serialize_terminal(t) for t in query.order_by(TerminalSmartApp.id).all()]


@router.delete("/terminais/{terminal_id}")
def revogar_terminal(terminal_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = db.query(TerminalSmartApp).filter(TerminalSmartApp.id == terminal_id)
    if not eh_admin(user):
        query = query.filter(TerminalSmartApp.cliente_id == user[2])
    terminal = query.first()
    if not terminal:
        raise HTTPException(status_code=404, detail="Terminal nao encontrado")
    terminal.ativo = False
    registrar_auditoria(
        db,
        user,
        acao="SMARTAPP_TERMINAL_REVOGADO",
        entidade_tipo="terminal_smartapp",
        entidade_id=terminal.id,
        descricao=f"Terminal SmartApp revogado nome={terminal.nome}",
    )
    db.commit()
    return _serialize_terminal(terminal)


# ---- App da Point ---------------------------------------------------------

@router.get("/maquinas/{machine_id}")
def obter_maquina(machine_id: str, db: Session = Depends(get_db), terminal=Depends(get_terminal)):
    maquina = svc.maquina_do_terminal(db, terminal, machine_id)
    produtos = db.query(Produto).filter(Produto.maquina_id == maquina.id_hardware).order_by(Produto.valor).all()
    return {
        "id_hardware": maquina.id_hardware,
        "nome": maquina.nome_local or maquina.id_hardware,
        "online": svc.maquina_online(maquina),
        "produtos": [{"id": p.id, "nome": p.nome, "valor": p.valor} for p in produtos],
    }


@router.post("/maquinas/{machine_id}/verificar-online")
def verificar_online(machine_id: str, db: Session = Depends(get_db), terminal=Depends(get_terminal)):
    maquina = svc.maquina_do_terminal(db, terminal, machine_id)
    return {"maquina_id": maquina.id_hardware, "online": svc.verificar_placa_online(db, maquina)}


@router.post("/pagamentos")
def confirmar_pagamento(dados: PagamentoSmartApp, db: Session = Depends(get_db), terminal=Depends(get_terminal)):
    maquina = svc.maquina_do_terminal(db, terminal, dados.maquina_id)
    payment_id = str(dados.payment_id).strip() if dados.payment_id is not None else None
    return svc.registrar_pagamento_e_liberar(
        db,
        terminal,
        maquina,
        payment_id=payment_id or None,
        payment_reference=dados.payment_reference.strip(),
        valor=dados.valor,
        metodo=dados.metodo,
        sn_terminal=dados.sn_terminal,
    )


@router.get("/comandos/{command_id}")
def status_comando(command_id: str, db: Session = Depends(get_db), terminal=Depends(get_terminal)):
    comando = (
        db.query(ComandoMaquina)
        .join(Maquina, Maquina.id_hardware == ComandoMaquina.maquina_id)
        .filter(ComandoMaquina.command_id == command_id, Maquina.cliente_id == terminal.cliente_id)
        .first()
    )
    if not comando:
        raise HTTPException(status_code=404, detail="Comando nao encontrado")
    return {"command_id": comando.command_id, "status": comando.status, "detalhe_status": comando.detalhe_status}

"""Rotas do banco "confrapag_sdk" (app CompactPay na maquininha PAX com o SDK ConfraPag).

- /confrapag/terminais...: painel (login JWT) cadastra e revoga a chave de cada maquininha.
- o resto: o proprio app, autenticado pelo header X-Terminal-Key.

Logica em app/services/confrapag.py. Nada aqui altera as rotas/fluxos existentes.
"""
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.escopo import eh_admin
from app.db.session import SessionLocal
from app.models.confrapag import TerminalConfrapag
from app.models.models import Cliente, ComandoMaquina, Maquina
from app.services import confrapag as svc
from app.services.auditoria import registrar_auditoria

router = APIRouter(prefix="/confrapag", tags=["confrapag"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_terminal(
    x_terminal_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> TerminalConfrapag:
    return svc.autenticar_terminal(db, x_terminal_key)


class TerminalCreate(BaseModel):
    nome: str = Field(min_length=1, max_length=80)
    cliente_id: int | None = None  # so admin escolhe; cliente usa o proprio
    cnpj: str | None = None  # vazio: usa o CNPJ do cliente


class PagamentoConfrapag(BaseModel):
    maquina_id: str
    produto_id: int | None = None
    valor: float = Field(gt=0)
    tid: str = Field(min_length=1, max_length=80)
    nsu: str | None = Field(default=None, max_length=80)
    referencia: str = Field(min_length=1, max_length=120)
    metodo: str | None = Field(default=None, max_length=20)
    bandeira: str | None = Field(default=None, max_length=40)
    autorizacao: str | None = Field(default=None, max_length=40)
    sn_terminal: str | None = Field(default=None, max_length=80)


def _serialize_terminal(terminal: TerminalConfrapag) -> dict:
    return {
        "id": terminal.id,
        "cliente_id": terminal.cliente_id,
        "nome": terminal.nome,
        "cnpj": terminal.cnpj,
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
    cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    cnpj = svc.so_digitos(dados.cnpj) or svc.so_digitos(cliente.cnpj)
    if not svc.cnpj_valido(cnpj):
        raise HTTPException(
            status_code=422,
            detail="Informe o CNPJ (ou CPF) do estabelecimento cadastrado na ConfraPag",
        )

    chave = svc.gerar_chave()
    terminal = TerminalConfrapag(
        cliente_id=cliente_id,
        nome=dados.nome.strip(),
        cnpj=cnpj,
        chave_prefixo=chave[:10],
        chave_hash=svc.hash_chave(chave),
        ativo=True,
    )
    db.add(terminal)
    db.flush()
    registrar_auditoria(
        db,
        user,
        acao="CONFRAPAG_TERMINAL_CRIADO",
        entidade_tipo="terminal_confrapag",
        entidade_id=terminal.id,
        descricao=f"Terminal ConfraPag criado nome={terminal.nome} cliente_id={cliente_id}",
    )
    db.commit()
    db.refresh(terminal)
    # A chave so aparece aqui; no banco fica so o hash.
    return {**_serialize_terminal(terminal), "chave": chave}


@router.get("/terminais")
def listar_terminais(db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = db.query(TerminalConfrapag)
    if not eh_admin(user):
        query = query.filter(TerminalConfrapag.cliente_id == user[2])
    return [_serialize_terminal(t) for t in query.order_by(TerminalConfrapag.id).all()]


@router.delete("/terminais/{terminal_id}")
def revogar_terminal(terminal_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = db.query(TerminalConfrapag).filter(TerminalConfrapag.id == terminal_id)
    if not eh_admin(user):
        query = query.filter(TerminalConfrapag.cliente_id == user[2])
    terminal = query.first()
    if not terminal:
        raise HTTPException(status_code=404, detail="Terminal nao encontrado")
    terminal.ativo = False
    registrar_auditoria(
        db,
        user,
        acao="CONFRAPAG_TERMINAL_REVOGADO",
        entidade_tipo="terminal_confrapag",
        entidade_id=terminal.id,
        descricao=f"Terminal ConfraPag revogado nome={terminal.nome}",
    )
    db.commit()
    return _serialize_terminal(terminal)


# ---- App da maquininha ----------------------------------------------------

@router.get("/terminal")
def dados_do_terminal(terminal=Depends(get_terminal)):
    """O app pega aqui o CNPJ que vai no User(identification, environment) do SDK."""
    return {"id": terminal.id, "nome": terminal.nome, "cnpj": terminal.cnpj}


@router.get("/maquinas/{machine_id}")
def obter_maquina(machine_id: str, db: Session = Depends(get_db), terminal=Depends(get_terminal)):
    maquina = svc.maquina_do_terminal(db, terminal, machine_id)
    return {
        "id_hardware": maquina.id_hardware,
        "nome": maquina.nome_local or maquina.id_hardware,
        "online": svc.maquina_online(maquina),
        "produtos": svc.produtos_da_maquina(db, maquina),
    }


class ValoresConfrapag(BaseModel):
    valores: list[float] = Field(min_length=1, max_length=12)


@router.put("/maquinas/{machine_id}/valores")
def salvar_valores(machine_id: str, dados: ValoresConfrapag, db: Session = Depends(get_db), terminal=Depends(get_terminal)):
    """Valores cadastrados na maquininha (tela de configuracoes, protegida por PIN)."""
    maquina = svc.maquina_do_terminal(db, terminal, machine_id)
    produtos = svc.salvar_valores(db, maquina, dados.valores)
    print(f"[ConfraPag] valores salvos pela maquininha terminal_app={terminal.id} machine={machine_id} valores={[p['valor'] for p in produtos]}")
    return {"maquina_id": maquina.id_hardware, "produtos": produtos}


@router.post("/maquinas/{machine_id}/verificar-online")
def verificar_online(machine_id: str, db: Session = Depends(get_db), terminal=Depends(get_terminal)):
    maquina = svc.maquina_do_terminal(db, terminal, machine_id)
    return {"maquina_id": maquina.id_hardware, "online": svc.verificar_placa_online(db, maquina)}


@router.post("/pagamentos")
def confirmar_pagamento(dados: PagamentoConfrapag, db: Session = Depends(get_db), terminal=Depends(get_terminal)):
    maquina = svc.maquina_do_terminal(db, terminal, dados.maquina_id)
    return svc.registrar_pagamento_e_liberar(
        db,
        terminal,
        maquina,
        tid=dados.tid,
        nsu=dados.nsu,
        referencia=dados.referencia.strip(),
        valor=dados.valor,
        metodo=dados.metodo,
        bandeira=dados.bandeira,
        autorizacao=dados.autorizacao,
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

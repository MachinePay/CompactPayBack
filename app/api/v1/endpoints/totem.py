"""Rotas do totem vending (tablet + agente local). Logica em app/services/totem.py.

- /totem/maquinas/...: painel (login JWT, regra de escopo de app/core/escopo.py).
- /totem/vincular, /totem/{id}/eventos, /totem/{id}/config: o agente da maquina
  (token proprio, header Authorization: Bearer ...).
- /totem/midias/{hash}.webp: fotos dos produtos (publicas, endereco = sha256).

Nada aqui altera rotas ou fluxos existentes. A Point do totem se lista e entra
em modo integrado pelas rotas que ja existem em /tela/point/... .
"""
from fastapi import APIRouter, Depends, File, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.escopo import obter_maquina_do_usuario
from app.db.session import SessionLocal
from app.models.totem import TotemMidia
from app.services import totem as svc
from app.services.auditoria import registrar_auditoria
from app.services.sumup import create_reader, get_sumup_merchant_code
from app.services import tela_point as tela_point_svc

router = APIRouter(prefix="/totem", tags=["totem-vending"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _totem_do_painel(db: Session, user, machine_id: str):
    maquina = obter_maquina_do_usuario(db, user, machine_id)
    totem = svc.obter_totem(db, maquina.id_hardware)
    if totem is None or not totem.ativo:
        raise HTTPException(status_code=404, detail="Totem nao ativado nesta maquina")
    return maquina, totem


class PagamentoTotem(BaseModel):
    modo_cartao: str = Field(pattern="^(off|mercadopago|sumup)$")
    mp_terminal_id: str | None = Field(default=None, max_length=120)
    sumup_reader_id: str | None = Field(default=None, max_length=120)


class ProdutoTotem(BaseModel):
    nome: str | None = Field(default=None, min_length=1, max_length=40)
    preco_centavos: int | None = Field(default=None, ge=0, le=10_000_000)
    imagem_hash: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    ativo: bool | None = None
    estoque: int | None = Field(default=None, ge=0, le=100_000)
    estoque_minimo: int | None = Field(default=None, ge=0, le=100_000)
    ordem: int | None = Field(default=None, ge=0, le=1000)


class PareamentoTotem(BaseModel):
    pairing_code: str = Field(min_length=4, max_length=20)


class VinculoTotem(BaseModel):
    code: str = Field(min_length=4, max_length=32)
    agentVersion: str | None = Field(default=None, max_length=40)
    boardVersion: str | None = Field(default=None, max_length=40)


class EventosTotem(BaseModel):
    events: list[dict] = Field(default_factory=list, max_length=500)


# ---- Painel -------------------------------------------------------------------

@router.get("/maquinas/{machine_id}")
def ver_totem(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    maquina = obter_maquina_do_usuario(db, user, machine_id)
    return svc.resumo(db, svc.obter_totem(db, maquina.id_hardware), maquina)


@router.post("/maquinas/{machine_id}/ativar")
def ativar_totem(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Liga o modo totem na maquina e gera o codigo de vinculo (mostrado uma vez)."""
    maquina = obter_maquina_do_usuario(db, user, machine_id)
    totem = svc.ativar(db, maquina)
    codigo = svc.gerar_codigo(db, totem)
    registrar_auditoria(db, user, acao="TOTEM_ATIVADO", entidade_tipo="maquina", entidade_id=maquina.id_hardware,
                        descricao="Totem vending ativado e codigo de vinculo gerado")
    db.commit()
    return {**svc.resumo(db, totem, maquina), **codigo}


@router.post("/maquinas/{machine_id}/codigo")
def novo_codigo(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    maquina, totem = _totem_do_painel(db, user, machine_id)
    codigo = svc.gerar_codigo(db, totem)
    registrar_auditoria(db, user, acao="TOTEM_CODIGO", entidade_tipo="maquina", entidade_id=maquina.id_hardware,
                        descricao="Novo codigo de vinculo do totem")
    db.commit()
    return {**svc.resumo(db, totem, maquina), **codigo}


@router.delete("/maquinas/{machine_id}")
def desativar_totem(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    maquina, totem = _totem_do_painel(db, user, machine_id)
    svc.desativar(db, totem)
    registrar_auditoria(db, user, acao="TOTEM_DESATIVADO", entidade_tipo="maquina", entidade_id=maquina.id_hardware,
                        descricao="Totem vending desativado (token revogado)")
    db.commit()
    return svc.resumo(db, totem, maquina)


@router.put("/maquinas/{machine_id}/pagamento")
def salvar_pagamento(machine_id: str, dados: PagamentoTotem, db: Session = Depends(get_db), user=Depends(get_current_user)):
    maquina, totem = _totem_do_painel(db, user, machine_id)
    svc.salvar_pagamento(db, totem, dados.modo_cartao, dados.mp_terminal_id, dados.sumup_reader_id)
    registrar_auditoria(db, user, acao="TOTEM_PAGAMENTO", entidade_tipo="maquina", entidade_id=maquina.id_hardware,
                        descricao=f"Maquininha do totem: {dados.modo_cartao}")
    db.commit()
    return svc.resumo(db, totem, maquina)


@router.post("/maquinas/{machine_id}/sumup/parear")
def parear_sumup(machine_id: str, dados: PareamentoTotem, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Pareia a Solo do totem pela Cloud API. Guarda o reader no totem (nao na
    maquina), para o poller da SumUp nao tratar essas vendas como pulso."""
    maquina, totem = _totem_do_painel(db, user, machine_id)
    dono = maquina.dono
    api_key = (dono.sumup_api_key or "").strip() if dono else ""
    if not api_key:
        raise HTTPException(status_code=422, detail="Cliente sem API key da SumUp")
    merchant = (dono.sumup_merchant_code or "").strip() or get_sumup_merchant_code(api_key)
    reader = create_reader(api_key, merchant, dados.pairing_code.strip(), name=f"Totem {maquina.id_hardware}")
    svc.salvar_pagamento(db, totem, "sumup", totem.mp_terminal_id, reader["id"])
    registrar_auditoria(db, user, acao="TOTEM_SUMUP_PAREADA", entidade_tipo="maquina", entidade_id=maquina.id_hardware,
                        descricao=f"Solo pareada no totem: {reader['id']}")
    db.commit()
    return {**svc.resumo(db, totem, maquina), "reader": reader}


@router.post("/maquinas/{machine_id}/caixa")
def criar_caixa(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Caixa e loja no Mercado Pago para o totem (mesma funcao das maquinas de tela).
    Para totem que nasceu antes de o cadastro criar o caixa sozinho."""
    maquina, totem = _totem_do_painel(db, user, machine_id)
    if maquina.banco_pagamento != "mercado_pago_totem":
        raise HTTPException(status_code=422, detail="So para maquinas com banco Mercado Pago (vending machine)")
    tinha = maquina.mp_pos_id is not None and str(maquina.mp_pos_id).strip() != ""
    caixa = tela_point_svc.criar_caixa(db, maquina)
    if not tinha:
        registrar_auditoria(db, user, acao="TOTEM_CAIXA_CRIADO", entidade_tipo="maquina", entidade_id=maquina.id_hardware,
                            descricao=f"Caixa Mercado Pago criado para o totem pos_id={caixa['pos_id']}")
        db.commit()
    return svc.resumo(db, totem, maquina)


@router.get("/maquinas/{machine_id}/produtos")
def listar_produtos(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    maquina, _ = _totem_do_painel(db, user, machine_id)
    return [svc.produto_dict(p) for p in svc.listar_produtos(db, maquina.id_hardware)]


@router.put("/maquinas/{machine_id}/produtos/{slot}")
def salvar_produto(machine_id: str, slot: int, dados: ProdutoTotem, db: Session = Depends(get_db), user=Depends(get_current_user)):
    maquina, totem = _totem_do_painel(db, user, machine_id)
    p = svc.salvar_produto(db, totem, slot, dados.model_dump(exclude_unset=True), por="painel")
    db.commit()
    return svc.produto_dict(p)


@router.post("/midias")
async def enviar_midia(arquivo: UploadFile = File(...), db: Session = Depends(get_db), user=Depends(get_current_user)):
    conteudo = await arquivo.read(svc.MIDIA_MAX + 1)
    h = svc.salvar_midia(db, user[2], conteudo)
    db.commit()
    return {"hash": h}


# ---- Fotos (publicas pelo hash) ----------------------------------------------

@router.get("/midias/{arquivo}")
def baixar_midia(arquivo: str, db: Session = Depends(get_db)):
    h = arquivo.removesuffix(".webp")
    if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
        raise HTTPException(status_code=404, detail="Imagem nao encontrada")
    midia = db.query(TotemMidia).filter(TotemMidia.hash == h).first()
    if midia is None:
        raise HTTPException(status_code=404, detail="Imagem nao encontrada")
    return Response(content=midia.conteudo, media_type="image/webp",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ---- Agente da maquina --------------------------------------------------------

@router.post("/vincular")
def vincular(dados: VinculoTotem, db: Session = Depends(get_db)):
    resultado = svc.vincular(db, dados.code, dados.agentVersion, dados.boardVersion)
    db.commit()
    return resultado


@router.post("/{machine_id}/eventos")
def receber_eventos(machine_id: str, dados: EventosTotem, authorization: str | None = Header(default=None),
                    db: Session = Depends(get_db)):
    totem = svc.autenticar(db, machine_id, authorization)
    aceitos = svc.processar_eventos(db, totem, dados.events)
    svc.registrar_contato(db, totem)
    db.commit()
    return {"accepted": aceitos}


@router.get("/{machine_id}/config")
def config(machine_id: str, version: str | None = None, authorization: str | None = Header(default=None),
           db: Session = Depends(get_db)):
    totem = svc.autenticar(db, machine_id, authorization)
    svc.registrar_contato(db, totem)
    corpo = svc.montar_config(db, totem)
    db.commit()
    if version and version == corpo["version"]:
        return Response(status_code=204)
    return corpo

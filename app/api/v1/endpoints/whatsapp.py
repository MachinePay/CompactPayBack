"""Tela Admin > WhatsApp: conexao da Evolution API, textos dos avisos,
clientes que recebem e historico de envios. So admin. Ver
app/services/whatsapp_alertas.py."""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import Cliente, Maquina
from app.models.whatsapp import WhatsappCliente, WhatsappEnvio
from app.services.auditoria import registrar_auditoria
from app.services.whatsapp_alertas import (
    MENSAGEM_OFFLINE_PADRAO,
    MENSAGEM_VOLTOU_PADRAO,
    VALORES_EXEMPLO,
    WhatsappErro,
    atualizar_config,
    conectar,
    conexao_para_tela,
    config_para_tela,
    enviar_texto,
    montar_mensagem,
    normalizar_telefone,
    obter_config,
    telefones_do_cliente,
)

router = APIRouter(prefix="/whatsapp")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _somente_admin(user):
    if user[1] != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode configurar o WhatsApp")


class ConfigIn(BaseModel):
    ativo: Optional[bool] = None
    evolution_url: Optional[str] = None
    api_key: Optional[str] = None
    instancia: Optional[str] = None
    minutos_offline: Optional[int] = None
    mensagem_offline: Optional[str] = None
    enviar_voltou: Optional[bool] = None
    mensagem_voltou: Optional[str] = None


class ClienteIn(BaseModel):
    ativo: bool


class TesteIn(BaseModel):
    telefone: str
    tipo: str = "offline"


@router.get("/config")
def ler_config(db: Session = Depends(get_db), user=Depends(get_current_user)):
    _somente_admin(user)
    config = obter_config(db)
    db.commit()
    return config_para_tela(config)


@router.put("/config")
def salvar_config(payload: ConfigIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _somente_admin(user)
    config = obter_config(db)
    dados = payload.model_dump(exclude_unset=True)
    try:
        atualizar_config(db, config, dados, user[0].email)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    campos = ", ".join(sorted(k for k in dados if k != "api_key")) + (", api_key" if dados.get("api_key") else "")
    registrar_auditoria(
        db, user, acao="WHATSAPP_CONFIG", entidade_tipo="whatsapp", entidade_id=1,
        descricao=f"Configuracao do WhatsApp alterada: {campos} (ativo={config.ativo})",
    )
    db.commit()
    return config_para_tela(config)


@router.get("/conexao")
def ler_conexao(db: Session = Depends(get_db), user=Depends(get_current_user)):
    _somente_admin(user)
    config = obter_config(db)
    db.commit()
    return conexao_para_tela(config)


@router.post("/conectar")
def conectar_numero(db: Session = Depends(get_db), user=Depends(get_current_user)):
    _somente_admin(user)
    config = obter_config(db)
    db.commit()
    try:
        return conectar(config)
    except WhatsappErro as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/teste")
def enviar_teste(payload: TesteIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _somente_admin(user)
    numero = normalizar_telefone(payload.telefone)
    if not numero:
        raise HTTPException(status_code=422, detail="Telefone invalido. Use DDD + numero.")
    config = obter_config(db)
    modelo = (
        config.mensagem_voltou or MENSAGEM_VOLTOU_PADRAO
        if payload.tipo == "voltou"
        else config.mensagem_offline or MENSAGEM_OFFLINE_PADRAO
    )
    texto = "[TESTE]\n\n" + montar_mensagem(modelo, VALORES_EXEMPLO)
    try:
        enviar_texto(config, numero, texto)
    except WhatsappErro as exc:
        db.add(WhatsappEnvio(tipo="teste", telefone=numero, mensagem=texto, status="erro", erro=str(exc)[:500]))
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc))
    db.add(WhatsappEnvio(tipo="teste", telefone=numero, mensagem=texto, status="enviado"))
    db.commit()
    return {"ok": True, "telefone": numero}


@router.get("/clientes")
def listar_clientes(db: Session = Depends(get_db), user=Depends(get_current_user)):
    _somente_admin(user)
    escolhas = {item.cliente_id: item for item in db.query(WhatsappCliente).all()}
    contagem = {}
    for (cliente_id,) in db.query(Maquina.cliente_id).filter(Maquina.cliente_id.isnot(None)):
        contagem[cliente_id] = contagem.get(cliente_id, 0) + 1
    resultado = []
    for cliente in db.query(Cliente).order_by(Cliente.nome_empresa).all():
        escolha = escolhas.get(cliente.id)
        resultado.append(
            {
                "cliente_id": cliente.id,
                "nome": cliente.nome_empresa,
                "ativo": bool(escolha and escolha.ativo),
                "numeros_envio": telefones_do_cliente(db, cliente),
                "maquinas": contagem.get(cliente.id, 0),
            }
        )
    return resultado


@router.put("/clientes/{cliente_id}")
def salvar_cliente(
    cliente_id: int, payload: ClienteIn, db: Session = Depends(get_db), user=Depends(get_current_user)
):
    _somente_admin(user)
    cliente = db.get(Cliente, cliente_id)
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    numeros = telefones_do_cliente(db, cliente)
    if payload.ativo and not numeros:
        raise HTTPException(
            status_code=422,
            detail="Cliente sem telefone valido no perfil. Cadastre o telefone com DDD em Usuarios.",
        )
    escolha = db.get(WhatsappCliente, cliente_id) or WhatsappCliente(cliente_id=cliente_id, ativo=False)
    agora = datetime.utcnow()
    if payload.ativo and not escolha.ativo:
        escolha.ativado_em = agora
    escolha.ativo = payload.ativo
    escolha.updated_at = agora
    db.add(escolha)
    registrar_auditoria(
        db, user, acao="WHATSAPP_CLIENTE", entidade_tipo="cliente", entidade_id=cliente_id,
        descricao=f"Aviso WhatsApp {'ativado' if payload.ativo else 'desativado'} numeros={','.join(numeros)}",
    )
    db.commit()
    return {"cliente_id": cliente_id, "ativo": escolha.ativo, "numeros_envio": numeros}


@router.get("/envios")
def listar_envios(limite: int = 100, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _somente_admin(user)
    limite = max(1, min(limite, 500))
    nomes = dict(db.query(Cliente.id, Cliente.nome_empresa).all())
    maquinas = dict(db.query(Maquina.id_hardware, Maquina.nome_local).all())
    envios = db.query(WhatsappEnvio).order_by(WhatsappEnvio.id.desc()).limit(limite).all()
    return [
        {
            "id": e.id,
            "tipo": e.tipo,
            "status": e.status,
            "erro": e.erro,
            "telefone": e.telefone,
            "mensagem": e.mensagem,
            "cliente_nome": nomes.get(e.cliente_id),
            "maquina_id": e.maquina_id,
            "maquina_nome": maquinas.get(e.maquina_id),
            "created_at": e.created_at,
        }
        for e in envios
    ]

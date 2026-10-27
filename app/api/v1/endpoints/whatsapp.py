"""Tela Admin > WhatsApp: conexao da Evolution API, tempos e textos dos
avisos, o que cada cliente recebe e historico de envios. So admin. Ver
app/services/whatsapp_alertas.py."""

from datetime import datetime
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import Cliente, Maquina
from app.models.whatsapp import WhatsappCliente, WhatsappEnvio
from app.services.auditoria import registrar_auditoria
from app.services.whatsapp_alertas import (
    PADROES,
    VALORES_EXEMPLO,
    WhatsappErro,
    atualizar_config,
    conectar,
    conexao_para_tela,
    config_para_tela,
    enviar_resumo_agora,
    enviar_texto,
    hora_local_agora,
    modelo_mensagem,
    montar_mensagem,
    normalizar_telefone,
    obter_config,
    telefones_do_cliente,
    validar_hora,
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
    lembrete_horas: Optional[int] = None
    resumo_horario: Optional[str] = None
    # {"offline": "...", "lembrete": "...", "voltou": "...", "resumo": "..."}
    mensagens: Optional[Dict[str, str]] = None


class ClienteIn(BaseModel):
    ativo: bool = False
    recebe_lembrete: bool = False
    recebe_voltou: bool = False
    resumo_diario: bool = False
    # Vazio = horario padrao da configuracao.
    resumo_horario: Optional[str] = None


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
    tipo = payload.tipo if payload.tipo in PADROES else "offline"
    config = obter_config(db)
    texto = "[TESTE]\n\n" + montar_mensagem(modelo_mensagem(config, tipo), VALORES_EXEMPLO[tipo])
    try:
        enviar_texto(config, numero, texto)
    except WhatsappErro as exc:
        db.add(WhatsappEnvio(tipo="teste", telefone=numero, mensagem=texto, status="erro", erro=str(exc)[:500]))
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc))
    db.add(WhatsappEnvio(tipo="teste", telefone=numero, mensagem=texto, status="enviado"))
    db.commit()
    return {"ok": True, "telefone": numero}


def _cliente_para_tela(db: Session, cliente: Cliente, escolha: WhatsappCliente | None, maquinas: int) -> dict:
    return {
        "cliente_id": cliente.id,
        "nome": cliente.nome_empresa,
        "ativo": bool(escolha and escolha.ativo),
        "recebe_lembrete": bool(escolha and escolha.recebe_lembrete),
        "recebe_voltou": bool(escolha and escolha.recebe_voltou),
        "resumo_diario": bool(escolha and escolha.resumo_diario),
        "resumo_horario": escolha.resumo_horario if escolha else None,
        "resumo_ultimo_dia": escolha.resumo_ultimo_dia if escolha else None,
        "numeros_envio": telefones_do_cliente(db, cliente),
        "maquinas": maquinas,
    }


@router.get("/clientes")
def listar_clientes(db: Session = Depends(get_db), user=Depends(get_current_user)):
    _somente_admin(user)
    escolhas = {item.cliente_id: item for item in db.query(WhatsappCliente).all()}
    contagem = {}
    for (cliente_id,) in db.query(Maquina.cliente_id).filter(Maquina.cliente_id.isnot(None)):
        contagem[cliente_id] = contagem.get(cliente_id, 0) + 1
    return [
        _cliente_para_tela(db, cliente, escolhas.get(cliente.id), contagem.get(cliente.id, 0))
        for cliente in db.query(Cliente).order_by(Cliente.nome_empresa).all()
    ]


@router.put("/clientes/{cliente_id}")
def salvar_cliente(
    cliente_id: int, payload: ClienteIn, db: Session = Depends(get_db), user=Depends(get_current_user)
):
    _somente_admin(user)
    cliente = db.get(Cliente, cliente_id)
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    algum = payload.ativo or payload.resumo_diario
    numeros = telefones_do_cliente(db, cliente)
    if algum and not numeros:
        raise HTTPException(
            status_code=422,
            detail="Cliente sem telefone valido no perfil. Cadastre o telefone com DDD em Usuarios.",
        )
    horario = (payload.resumo_horario or "").strip() or None
    if horario:
        try:
            horario = validar_hora(horario, "Horario do resumo")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    escolha = db.get(WhatsappCliente, cliente_id) or WhatsappCliente(cliente_id=cliente_id, ativo=False)
    agora = datetime.utcnow()
    if payload.ativo and not escolha.ativo:
        escolha.ativado_em = agora
    escolha.ativo = payload.ativo
    # Lembrete e "voltou" dependem do aviso de offline.
    escolha.recebe_lembrete = payload.ativo and payload.recebe_lembrete
    escolha.recebe_voltou = payload.ativo and payload.recebe_voltou
    # Mudou o horario (ou ligou o resumo) para mais tarde hoje: volta a valer
    # hoje, mesmo que um horario anterior ja tenha disparado.
    horario_efetivo = horario or obter_config(db).resumo_horario or "22:00"
    mudou = horario != escolha.resumo_horario or (payload.resumo_diario and not escolha.resumo_diario)
    if payload.resumo_diario and mudou and horario_efetivo > hora_local_agora():
        escolha.resumo_ultimo_dia = None
    escolha.resumo_diario = payload.resumo_diario
    escolha.resumo_horario = horario
    escolha.updated_at = agora
    db.add(escolha)
    registrar_auditoria(
        db, user, acao="WHATSAPP_CLIENTE", entidade_tipo="cliente", entidade_id=cliente_id,
        descricao=(
            f"WhatsApp: offline={escolha.ativo} lembrete={escolha.recebe_lembrete} voltou={escolha.recebe_voltou} "
            f"resumo={escolha.resumo_diario}@{horario or 'padrao'} numeros={','.join(numeros)}"
        ),
    )
    db.commit()
    return _cliente_para_tela(db, cliente, escolha, db.query(Maquina).filter(Maquina.cliente_id == cliente_id).count())


@router.post("/clientes/{cliente_id}/resumo-agora")
def resumo_agora(cliente_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _somente_admin(user)
    cliente = db.get(Cliente, cliente_id)
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    try:
        enviados = enviar_resumo_agora(db, cliente)
    except WhatsappErro as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True, "enviados": enviados}


@router.get("/envios")
def listar_envios(limite: int = 100, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _somente_admin(user)
    limite = max(1, min(limite, 500))
    nomes = dict(db.query(Cliente.id, Cliente.nome_empresa).all())
    maquinas = dict(db.query(Maquina.id_hardware, Maquina.nome_local).all())
    envios = db.query(WhatsappEnvio).order_by(WhatsappEnvio.id.desc()).limit(limite).all()
    resultado = []
    for e in envios:
        ids = [i for i in (e.maquina_id or "").split(",") if i]
        resultado.append(
            {
                "id": e.id,
                "tipo": e.tipo,
                "status": e.status,
                "erro": e.erro,
                "telefone": e.telefone,
                "mensagem": e.mensagem,
                "cliente_nome": nomes.get(e.cliente_id),
                "maquinas": ", ".join(maquinas.get(i) or i for i in ids),
                "created_at": e.created_at,
            }
        )
    return resultado

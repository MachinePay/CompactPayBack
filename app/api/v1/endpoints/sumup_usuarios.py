"""Usuario SumUp de cada maquininha (para identificar o Pix).

O Pix feito direto na maquininha SumUp nao traz o codigo do reader - o unico
dado que diz de qual maquininha veio e' o usuario logado nela (campo "user").
Aqui o sistema cria um usuario gerenciado por maquininha na conta SumUp do
cliente (API de membros, sem convite por e-mail), grava na maquina e mostra o
login/senha para logar a maquininha. So admin; tudo vai para a auditoria.
"""
import secrets
import string
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.escopo import eh_admin, obter_maquina_do_usuario
from app.db.session import SessionLocal
from app.models.models import Cliente
from app.services.auditoria import registrar_auditoria
from app.services.sumup import create_managed_member
from app.services.wifi_credenciais import criptografar_senha, descriptografar_senha

router = APIRouter()

DOMINIO_USUARIOS = "compactpay.com.br"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _exigir_admin(user) -> None:
    if not eh_admin(user):
        raise HTTPException(status_code=403, detail="Apenas admin pode gerenciar o usuario SumUp da maquininha")


def gerar_senha(tamanho: int = 14) -> str:
    """Senha forte, so letras e numeros (facil de digitar na maquininha)."""
    alfabeto = string.ascii_letters + string.digits
    while True:
        senha = "".join(secrets.choice(alfabeto) for _ in range(tamanho))
        if any(c.isupper() for c in senha) and any(c.islower() for c in senha) and any(c.isdigit() for c in senha):
            return senha


@router.post("/maquinas/{machine_id}/sumup/usuario")
def criar_usuario_da_maquininha(
    machine_id: str,
    payload: dict | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Cria o usuario da maquininha na conta SumUp do cliente. Se a maquina ja
    tem um usuario criado aqui, so recria com {"recriar": true}."""
    _exigir_admin(user)
    maquina = obter_maquina_do_usuario(db, user, machine_id)
    if (maquina.banco_pagamento or "") != "sumup":
        raise HTTPException(status_code=422, detail="Esta maquina nao usa SumUp")
    cliente = db.query(Cliente).filter(Cliente.id == maquina.cliente_id).first()
    if not cliente or not (cliente.sumup_api_key and cliente.sumup_merchant_code):
        raise HTTPException(status_code=422, detail="O cliente desta maquina nao tem a chave da SumUp cadastrada")
    if maquina.sumup_usuario_senha_cript and not (payload or {}).get("recriar"):
        raise HTTPException(
            status_code=409,
            detail="Esta maquina ja tem um usuario SumUp criado pelo sistema. Use 'Ver login' ou recrie.",
        )

    email = f"maquina{machine_id}.{secrets.token_hex(3)}@{DOMINIO_USUARIOS}".lower()
    senha = gerar_senha()
    try:
        membro = create_managed_member(
            cliente.sumup_api_key.strip(),
            cliente.sumup_merchant_code.strip(),
            email,
            senha,
            nickname=f"Maquina {machine_id} - {maquina.nome_local or ''}".strip(" -"),
        )
    except HTTPException as exc:
        detalhe = str(exc.detail)
        if " 401" in detalhe or " 403" in detalhe:
            detalhe = (
                "A chave da SumUp deste cliente nao tem permissao para criar usuarios (escopo members.write). "
                "Gere uma chave com essa permissao no painel da SumUp e atualize o cliente. "
                f"Resposta da SumUp: {exc.detail}"
            )
        raise HTTPException(status_code=502, detail=detalhe) from None

    maquina.sumup_usuario = ((membro.get("user") or {}).get("email") or email).lower()
    maquina.sumup_usuario_senha_cript = criptografar_senha(senha)
    maquina.sumup_usuario_member_id = membro.get("id")
    registrar_auditoria(
        db,
        user,
        "sumup_usuario_criado",
        "maquina",
        machine_id,
        f"Criou o usuario SumUp {maquina.sumup_usuario} (membro {maquina.sumup_usuario_member_id}) para a maquininha",
    )
    db.commit()
    return {
        "usuario": maquina.sumup_usuario,
        "senha": senha,
        "member_id": maquina.sumup_usuario_member_id,
        "criado_em": datetime.utcnow(),
    }


@router.get("/maquinas/{machine_id}/sumup/usuario")
def ver_login_da_maquininha(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Login/senha do usuario da maquininha criado pelo sistema (para logar a
    maquininha de novo). Acesso fica na auditoria."""
    _exigir_admin(user)
    maquina = obter_maquina_do_usuario(db, user, machine_id)
    if not maquina.sumup_usuario_senha_cript:
        return {"usuario": maquina.sumup_usuario, "senha": None, "criado_pelo_sistema": False}
    senha = descriptografar_senha(maquina.sumup_usuario_senha_cript)
    registrar_auditoria(
        db, user, "sumup_usuario_visto", "maquina", machine_id, f"Viu o login do usuario SumUp {maquina.sumup_usuario}"
    )
    db.commit()
    return {"usuario": maquina.sumup_usuario, "senha": senha, "criado_pelo_sistema": True}

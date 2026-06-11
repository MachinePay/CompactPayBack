from datetime import datetime, timedelta
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import get_current_user
from app.core.security import ALGORITHM, SECRET_KEY
from app.db.session import SessionLocal
from app.models.models import Cliente, Usuario
from app.services.mercado_pago import exchange_oauth_code, mp_request, search_store_by_external_id

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _redirect_uri() -> str:
    if settings.MP_OAUTH_REDIRECT_URI:
        return settings.MP_OAUTH_REDIRECT_URI
    raise HTTPException(status_code=500, detail="MP_OAUTH_REDIRECT_URI nao configurado")


def _mp_validation_response(cliente: Cliente, checks: list[dict], **extra):
    blocking = [item for item in checks if not item.get("ok") and item.get("severity", "error") == "error"]
    warnings = [item for item in checks if not item.get("ok") and item.get("severity") == "warning"]
    return {
        "ok": not blocking,
        "cliente_id": cliente.id,
        "cliente_nome": cliente.nome_empresa,
        "mp_user_id": cliente.mp_user_id,
        "mp_live_mode": bool(cliente.mp_live_mode),
        "mp_scope": cliente.mp_scope,
        "mp_store_id": cliente.mp_store_id,
        "mp_store_external_id": cliente.mp_store_external_id,
        "mp_pos_category": cliente.mp_pos_category,
        "blocking_count": len(blocking),
        "warning_count": len(warnings),
        "checks": checks,
        **extra,
    }


def _mp_status_value(status) -> str:
    if isinstance(status, dict):
        return str(status.get("id") or status.get("status") or status.get("description") or "").strip()
    return str(status or "").strip()


@router.get("/mercado-pago/oauth/url")
def gerar_url_oauth_mercado_pago(
    cliente_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode vincular Mercado Pago")
    if not settings.MP_APP_ID:
        raise HTTPException(status_code=500, detail="MP_APP_ID nao configurado")
    cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")

    state = jwt.encode(
        {
            "cliente_id": cliente.id,
            "exp": datetime.utcnow() + timedelta(minutes=15),
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    params = urllib.parse.urlencode(
        {
            "client_id": settings.MP_APP_ID,
            "response_type": "code",
            "platform_id": "mp",
            "state": state,
            "redirect_uri": _redirect_uri(),
        }
    )
    return {"url": f"https://auth.mercadopago.com.br/authorization?{params}"}


@router.get("/mercado-pago/oauth/callback")
def callback_oauth_mercado_pago(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    frontend_url = settings.FRONTEND_URL.rstrip("/")
    if error:
        return RedirectResponse(f"{frontend_url}/usuarios?mp_status=erro&detail={urllib.parse.quote(error)}")
    if not code or not state:
        return RedirectResponse(f"{frontend_url}/usuarios?mp_status=erro&detail=callback_invalido")

    try:
        payload = jwt.decode(state, SECRET_KEY, algorithms=[ALGORITHM])
        cliente_id = int(payload.get("cliente_id"))
    except (JWTError, TypeError, ValueError):
        return RedirectResponse(f"{frontend_url}/usuarios?mp_status=erro&detail=state_invalido")

    cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    if not cliente:
        return RedirectResponse(f"{frontend_url}/usuarios?mp_status=erro&detail=cliente_nao_encontrado")

    token_data = exchange_oauth_code(code, _redirect_uri())
    now = datetime.utcnow()
    expires_in = int(token_data.get("expires_in") or 0)
    cliente.mp_access_token = token_data.get("access_token")
    cliente.mp_public_key = token_data.get("public_key") or cliente.mp_public_key
    cliente.mp_refresh_token = token_data.get("refresh_token")
    cliente.mp_user_id = str(token_data.get("user_id") or cliente.mp_user_id or "")
    cliente.mp_token_expires_at = now + timedelta(seconds=expires_in) if expires_in else None
    cliente.mp_live_mode = bool(token_data.get("live_mode"))
    cliente.mp_scope = token_data.get("scope")
    cliente.mp_client_id = settings.MP_APP_ID
    usuarios = db.query(Usuario).filter(Usuario.cliente_id == cliente.id).all()
    for usuario in usuarios:
        usuario.mp_access_token = cliente.mp_access_token
        usuario.mp_public_key = cliente.mp_public_key
        usuario.mp_refresh_token = cliente.mp_refresh_token
        usuario.mp_user_id = cliente.mp_user_id
        usuario.mp_token_expires_at = cliente.mp_token_expires_at
        usuario.mp_live_mode = cliente.mp_live_mode
        usuario.mp_scope = cliente.mp_scope
        usuario.mp_client_id = cliente.mp_client_id
    db.commit()

    return RedirectResponse(f"{frontend_url}/usuarios?mp_status=conectado&cliente_id={cliente.id}")


@router.get("/mercado-pago/clientes/{cliente_id}/validacao")
def validar_integracao_mercado_pago(
    cliente_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode validar Mercado Pago")

    cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")

    checks = []

    mp_habilitado = bool(cliente.cliente_mercado_pago or cliente.mp_access_token)
    checks.append({
        "key": "cliente_mercado_pago",
        "label": "Cliente Mercado Pago",
        "ok": mp_habilitado,
        "severity": "error",
        "message": "Habilitado" if mp_habilitado else "Mercado Pago nao esta habilitado para este cliente",
        "hint": "Ative o checkbox Cliente Mercado Pago no cadastro do usuario cliente.",
    })

    access_token = (cliente.mp_access_token or "").strip()
    checks.append({
        "key": "mp_access_token",
        "label": "Access token",
        "ok": bool(access_token),
        "severity": "error",
        "message": "Token cadastrado" if access_token else "Cadastre ou conecte o Mercado Pago antes de criar maquina",
        "hint": "Use o botao Conectar MP ou preencha um token de producao valido.",
    })

    checks.append({
        "key": "mp_public_key",
        "label": "Public key",
        "ok": bool((cliente.mp_public_key or "").strip()),
        "severity": "warning",
        "message": "Public key cadastrada" if cliente.mp_public_key else "Public key nao cadastrada",
        "hint": "Nao bloqueia a criacao do POS, mas ajuda em fluxos futuros no frontend.",
    })

    category_ok = bool(cliente.mp_pos_category)
    checks.append({
        "key": "mp_pos_category",
        "label": "Categoria/MCC do caixa",
        "ok": category_ok,
        "severity": "warning",
        "message": f"Categoria configurada: {cliente.mp_pos_category}" if category_ok else "Categoria nao configurada; sera usado fallback do backend",
        "hint": "Mantenha 7994 enquanto estiver funcionando; altere somente se o Mercado Pago exigir outro MCC.",
    })

    if not access_token:
        return _mp_validation_response(cliente, checks, next_step="Conecte ou cadastre o Access Token do Mercado Pago.")

    try:
        mp_user = mp_request("GET", "https://api.mercadopago.com/users/me", access_token)
    except HTTPException as exc:
        checks.append({
            "key": "mp_users_me",
            "label": "Consulta Mercado Pago",
            "ok": False,
            "severity": "error",
            "message": str(exc.detail),
            "hint": "Confira se o token e de producao, nao expirou e pertence ao cliente correto.",
        })
        return _mp_validation_response(cliente, checks, next_step="Corrija o Access Token e valide novamente.")

    mp_user_id = str(mp_user.get("id") or "")
    if mp_user_id and cliente.mp_user_id != mp_user_id:
        cliente.mp_user_id = mp_user_id
        usuarios = db.query(Usuario).filter(Usuario.cliente_id == cliente.id).all()
        for usuario in usuarios:
            usuario.mp_user_id = mp_user_id
        db.commit()

    checks.append({
        "key": "mp_users_me",
        "label": "Consulta Mercado Pago",
        "ok": bool(mp_user_id),
        "severity": "error",
        "message": f"Conta Mercado Pago encontrada: {mp_user_id}" if mp_user_id else "Mercado Pago nao retornou user_id",
        "hint": "Esse user_id sera usado para procurar/criar lojas e caixas.",
    })

    mp_status = _mp_status_value(mp_user.get("status")) or "active"
    checks.append({
        "key": "mp_account_status",
        "label": "Status da conta",
        "ok": mp_status in {"active", "confirmed"},
        "severity": "warning",
        "message": f"Status: {mp_status or 'nao informado'}",
        "hint": "Se houver erro ao criar loja/caixa, confirme pendencias diretamente no Mercado Pago.",
    })

    checks.append({
        "key": "mp_live_mode",
        "label": "Ambiente",
        "ok": bool(cliente.mp_live_mode) or str(access_token).startswith("APP_USR-"),
        "severity": "warning",
        "message": "Token de producao" if (cliente.mp_live_mode or str(access_token).startswith("APP_USR-")) else "Token pode ser de teste",
        "hint": "Para maquina real, use token de producao da conta vinculada.",
    })

    if cliente.mp_store_external_id:
        store = search_store_by_external_id(mp_user_id, access_token, cliente.mp_store_external_id)
        checks.append({
            "key": "mp_store",
            "label": "Loja Mercado Pago",
            "ok": bool(store),
            "severity": "error",
            "message": "Loja encontrada" if store else "Loja salva no sistema nao foi encontrada no Mercado Pago",
            "hint": "Se a loja foi removida no Mercado Pago, limpe a loja salva ou crie uma nova maquina para gerar outra loja.",
        })
    else:
        checks.append({
            "key": "mp_store",
            "label": "Loja Mercado Pago",
            "ok": True,
            "severity": "warning",
            "message": "Sem loja padrao salva; a loja da maquina sera criada no cadastro",
            "hint": "Isso e esperado no primeiro cadastro de maquina do cliente.",
        })

    return _mp_validation_response(
        cliente,
        checks,
        mp_user_id=mp_user_id or cliente.mp_user_id,
        mp_account={
            "id": mp_user_id,
            "nickname": mp_user.get("nickname"),
            "email": mp_user.get("email"),
            "site_id": mp_user.get("site_id"),
            "status": mp_user.get("status"),
        },
        next_step="Integracao pronta para criar maquinas." if not [item for item in checks if not item.get("ok") and item.get("severity", "error") == "error"] else "Corrija os itens bloqueantes e valide novamente.",
    )

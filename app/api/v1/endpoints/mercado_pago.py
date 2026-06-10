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
        "message": "Habilitado" if mp_habilitado else "Mercado Pago nao esta habilitado para este cliente",
    })

    access_token = (cliente.mp_access_token or "").strip()
    checks.append({
        "key": "mp_access_token",
        "label": "Access token",
        "ok": bool(access_token),
        "message": "Token cadastrado" if access_token else "Cadastre ou conecte o Mercado Pago antes de criar maquina",
    })

    if not access_token:
        return {
            "ok": False,
            "cliente_id": cliente.id,
            "cliente_nome": cliente.nome_empresa,
            "mp_user_id": cliente.mp_user_id,
            "mp_live_mode": bool(cliente.mp_live_mode),
            "mp_store_id": cliente.mp_store_id,
            "mp_store_external_id": cliente.mp_store_external_id,
            "checks": checks,
        }

    try:
        mp_user = mp_request("GET", "https://api.mercadopago.com/users/me", access_token)
    except HTTPException as exc:
        checks.append({
            "key": "mp_users_me",
            "label": "Consulta Mercado Pago",
            "ok": False,
            "message": str(exc.detail),
        })
        return {
            "ok": False,
            "cliente_id": cliente.id,
            "cliente_nome": cliente.nome_empresa,
            "mp_user_id": cliente.mp_user_id,
            "mp_live_mode": bool(cliente.mp_live_mode),
            "mp_store_id": cliente.mp_store_id,
            "mp_store_external_id": cliente.mp_store_external_id,
            "checks": checks,
        }

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
        "message": f"Conta Mercado Pago encontrada: {mp_user_id}" if mp_user_id else "Mercado Pago nao retornou user_id",
    })

    if cliente.mp_store_external_id:
        store = search_store_by_external_id(mp_user_id, access_token, cliente.mp_store_external_id)
        checks.append({
            "key": "mp_store",
            "label": "Loja Mercado Pago",
            "ok": bool(store),
            "message": "Loja encontrada" if store else "Loja salva no sistema nao foi encontrada no Mercado Pago",
        })
    else:
        checks.append({
            "key": "mp_store",
            "label": "Loja Mercado Pago",
            "ok": True,
            "message": "Sem loja padrao salva; a loja da maquina sera criada no cadastro",
        })

    category_ok = bool(cliente.mp_pos_category)
    checks.append({
        "key": "mp_pos_category",
        "label": "Categoria/MCC do caixa",
        "ok": category_ok,
        "message": f"Categoria configurada: {cliente.mp_pos_category}" if category_ok else "Categoria nao configurada; sera usado fallback do backend",
    })

    return {
        "ok": all(item["ok"] for item in checks),
        "cliente_id": cliente.id,
        "cliente_nome": cliente.nome_empresa,
        "mp_user_id": mp_user_id or cliente.mp_user_id,
        "mp_live_mode": bool(cliente.mp_live_mode),
        "mp_store_id": cliente.mp_store_id,
        "mp_store_external_id": cliente.mp_store_external_id,
        "checks": checks,
    }

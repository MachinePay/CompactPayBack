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
from app.services.mercado_pago import exchange_oauth_code

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

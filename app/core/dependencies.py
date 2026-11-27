import hashlib
import hmac
import os
import re

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from app.schemas.token import TokenData
from app.core.security import SECRET_KEY, ALGORITHM

# auto_error=False: sem token cai no 401 abaixo do mesmo jeito, mas deixa a
# chave da IA (X-API-Key) passar sem o cabecalho Authorization.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/login", auto_error=False)

# Acesso da IA Central (ia-central) por chave de API, sem login e sem token
# vencendo: IA_CENTRAL_API_KEY no Render, enviada no cabecalho X-API-Key.
# Entra como admin; le qualquer rota (GET) e so escreve no que a IA ja usa
# (credito remoto e ping da maquina). Qualquer outra escrita e' recusada.
EMAIL_IA = "ia-central@integracao.compactpay"
ESCRITAS_IA = [
    re.compile(r"^/api/v1/maquinas/[^/]+/credito-teste$"),
    re.compile(r"^/api/v1/maquinas/[^/]+/verificar-online$"),
]


def _usuario_ia(request: Request, chave: str):
    esperado = os.getenv("IA_CENTRAL_API_KEY", "")
    a = hashlib.sha256(chave.encode()).digest()
    b = hashlib.sha256(esperado.encode()).digest()
    if len(esperado) < 32 or not hmac.compare_digest(a, b):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Chave invalida")
    caminho = request.url.path.rstrip("/")
    if request.method not in ("GET", "HEAD", "OPTIONS") and not any(r.match(caminho) for r in ESCRITAS_IA):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chave da IA so le; esta escrita nao esta liberada.")
    return TokenData(email=EMAIL_IA), "admin", None


def get_current_user(request: Request, token: str | None = Depends(oauth2_scheme)):
    chave = request.headers.get("x-api-key")
    if chave:
        return _usuario_ia(request, chave)
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Não autenticado",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exception
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        role: str = payload.get("role")
        cliente_id = payload.get("cliente_id")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    # Regra de acesso (app/core/escopo.py): quem nao e' admin so ve as maquinas
    # do proprio cliente. Login de cliente sem cliente vinculado e' recusado
    # aqui, senao os filtros "Maquina.cliente_id == cliente_id" virariam
    # "IS NULL" e mostrariam maquinas sem dono.
    if role != "admin" and cliente_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usuario sem cliente vinculado. Fale com o administrador.",
        )
    return TokenData(email=email), role, cliente_id

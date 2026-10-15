from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from app.schemas.token import TokenData
from app.core.security import SECRET_KEY, ALGORITHM

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/login")

def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Não autenticado",
        headers={"WWW-Authenticate": "Bearer"},
    )
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

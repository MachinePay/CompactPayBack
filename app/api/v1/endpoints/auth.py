from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.models.models import Usuario
from app.core import login_limite
from app.core.security import verify_password, create_access_token
from app.schemas.token import Token

router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/login", response_model=Token)
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    ip = login_limite.ip_do_pedido(request)
    minutos = login_limite.minutos_bloqueado(form_data.username, ip)
    if minutos:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Muitas tentativas de login. Tente novamente em {minutos} minuto(s).",
            headers={"Retry-After": str(minutos * 60)},
        )
    user = db.query(Usuario).filter(Usuario.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        login_limite.registrar_falha(form_data.username, ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais inválidas")
    login_limite.registrar_sucesso(form_data.username)
    access_token = create_access_token(data={"sub": user.email, "role": user.role.value, "cliente_id": user.cliente_id})
    return {"access_token": access_token, "token_type": "bearer"}

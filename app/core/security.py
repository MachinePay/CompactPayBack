import logging
import os
from datetime import datetime, timedelta

from jose import jwt
from passlib.context import CryptContext

from app.core.ambiente import em_producao

DEFAULT_SECRET_KEY = "compactpay-secret-key"
SECRET_KEY = os.getenv("SECRET_KEY", DEFAULT_SECRET_KEY)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

# Em producao, sem SECRET_KEY propria o backend nem sobe: com a chave padrao
# (que esta no codigo) qualquer um fabricaria um login de admin.
if em_producao() and SECRET_KEY == DEFAULT_SECRET_KEY:
    raise RuntimeError("SECRET_KEY padrao em producao. Configure SECRET_KEY no ambiente.")
if SECRET_KEY == DEFAULT_SECRET_KEY:
    logging.warning("SECRET_KEY padrao em uso. Configure SECRET_KEY no ambiente de producao.")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

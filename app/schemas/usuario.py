from typing import Optional

from pydantic import BaseModel, EmailStr


class UsuarioBase(BaseModel):
    email: EmailStr
    role: str = "cliente"
    cliente_id: Optional[int] = None
    nome: Optional[str] = None
    telefone: Optional[str] = None
    cpf: Optional[str] = None
    cnpj: Optional[str] = None
    endereco_rua: Optional[str] = None
    endereco_numero: Optional[str] = None
    endereco_cidade: Optional[str] = None
    endereco_estado: Optional[str] = None
    endereco_latitude: Optional[float] = None
    endereco_longitude: Optional[float] = None
    mp_public_key: Optional[str] = None
    mp_access_token: Optional[str] = None
    mp_client_id: Optional[str] = None
    mp_client_secret: Optional[str] = None
    mp_user_id: Optional[str] = None
    mp_store_id: Optional[str] = None
    mp_store_external_id: Optional[str] = None
    mp_live_mode: Optional[bool] = None
    mp_scope: Optional[str] = None
    mp_pos_category: Optional[int] = None


class UsuarioCreate(UsuarioBase):
    password: str


class UsuarioUpdate(UsuarioBase):
    password: Optional[str] = None


class UsuarioOut(UsuarioBase):
    id: int
    mp_configurado: bool = False
    mp_access_token: Optional[str] = None
    mp_client_secret: Optional[str] = None

    class Config:
        from_attributes = True

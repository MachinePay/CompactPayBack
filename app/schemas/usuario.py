from typing import Optional

from pydantic import BaseModel, EmailStr


class UsuarioBase(BaseModel):
    email: EmailStr
    role: str = "cliente"
    cliente_id: Optional[int] = None
    nome: Optional[str] = None


class UsuarioCreate(UsuarioBase):
    password: str


class UsuarioUpdate(UsuarioBase):
    password: Optional[str] = None


class UsuarioOut(UsuarioBase):
    id: int

    class Config:
        from_attributes = True

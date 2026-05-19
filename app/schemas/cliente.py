from typing import List, Optional

from pydantic import BaseModel


class ClienteBase(BaseModel):
    nome_empresa: str
    email_contato: str


class ClienteOut(ClienteBase):
    id: int
    maquinas: Optional[List[str]] = None

    class Config:
        from_attributes = True


class ClienteListOut(ClienteBase):
    id: int
    telefone: Optional[str] = None
    cpf: Optional[str] = None
    cnpj: Optional[str] = None
    endereco_rua: Optional[str] = None
    endereco_numero: Optional[str] = None
    endereco_cidade: Optional[str] = None
    endereco_estado: Optional[str] = None
    endereco_latitude: Optional[float] = None
    endereco_longitude: Optional[float] = None
    mp_configurado: bool = False
    mp_user_id: Optional[str] = None
    mp_store_id: Optional[str] = None
    mp_store_external_id: Optional[str] = None

    class Config:
        from_attributes = True

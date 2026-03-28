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

    class Config:
        from_attributes = True

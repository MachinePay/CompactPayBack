from pydantic import BaseModel
from typing import List, Optional

class ClienteBase(BaseModel):
    id: str
    nome_empresa: str
    email_contato: str
    api_key: str

class ClienteOut(ClienteBase):
    maquinas: Optional[List[str]] = None
    class Config:
        orm_mode = True

from pydantic import BaseModel
from typing import Optional

class ProdutoBase(BaseModel):
    nome: str
    valor: float
    maquina_id: str
    usuario_id: int

class ProdutoCreate(ProdutoBase):
    pass

class ProdutoOut(ProdutoBase):
    id: int
    class Config:
        orm_mode = True

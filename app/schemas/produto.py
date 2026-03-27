from pydantic import BaseModel
from typing import Optional

class ProdutoBase(BaseModel):
    nome: str
    valor: float
    maquina_id: str
    usuario_id: Optional[int] = None

class ProdutoCreate(ProdutoBase):
    pass

class ProdutoOut(ProdutoBase):
    id: int
    maquina_nome: Optional[str] = None

    class Config:
        from_attributes = True

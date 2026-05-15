from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class PagamentoCreate(BaseModel):
    maquina_id: str
    valor: float
    produto_id: Optional[int] = None
    descricao: Optional[str] = None


class PagamentoOut(BaseModel):
    ok: bool
    maquina_id: str
    valor: float
    produto_id: Optional[int] = None
    pulsos: int
    payload: str
    data_hora: datetime

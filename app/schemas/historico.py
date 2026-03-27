from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class HistoricoOperacaoOut(BaseModel):
    id: int
    maquina_id: str
    categoria: str
    descricao: str
    valor: Optional[float] = None
    created_at: datetime

    class Config:
        from_attributes = True

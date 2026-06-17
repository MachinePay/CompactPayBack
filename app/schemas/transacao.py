from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class TransacaoOut(BaseModel):
    id: int
    maquina_id: str
    maquina_nome: Optional[str] = None
    tipo: str
    metodo: str
    valor: float
    taxa: Optional[float] = None
    data_hora: datetime

    class Config:
        from_attributes = True

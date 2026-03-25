from pydantic import BaseModel
from datetime import datetime

class TransacaoBase(BaseModel):
    id: int
    maquina_id: str
    tipo: str
    valor: float
    timestamp: datetime

class TransacaoOut(TransacaoBase):
    class Config:
        orm_mode = True

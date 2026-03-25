from pydantic import BaseModel
from datetime import datetime
from typing import Literal

class TransacaoBase(BaseModel):
    machine_id: str
    valor: float
    tipo: Literal["IN", "OUT"]
    timestamp: datetime

class TransacaoCreate(BaseModel):
    machine_id: str
    valor: float
    tipo: Literal["IN", "OUT"]

class TransacaoOut(TransacaoBase):
    class Config:
        orm_mode = True

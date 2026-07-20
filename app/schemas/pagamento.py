from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


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
    topic: Optional[str] = None
    payload: str
    command_id: Optional[str] = None
    pulse_status: Optional[str] = None
    data_hora: datetime


class CreditoDigitalCreate(BaseModel):
    maquina_id: str
    pulsos: int = Field(gt=0)
    origem: str = "agarramais"
    referencia_externa: Optional[str] = None
    valor: Optional[float] = None


class CreditoDigitalOut(BaseModel):
    ok: bool
    maquina_id: str
    pulsos: int
    topic: Optional[str] = None
    payload: str
    command_id: str
    pulse_status: str
    referencia_externa: Optional[str] = None
    data_hora: datetime

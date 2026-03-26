from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class MaquinaCreate(BaseModel):
    id_hardware: str
    nome: str
    cliente_id: Optional[int] = None
    localizacao: Optional[str] = None


class MaquinaOut(BaseModel):
    id_hardware: str
    cliente_id: Optional[int] = None
    nome: Optional[str] = None
    localizacao: Optional[str] = None
    ultimo_sinal: Optional[datetime] = None
    status_online: bool = False
    faturamento: float = 0.0

    class Config:
        from_attributes = True

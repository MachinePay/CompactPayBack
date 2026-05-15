from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class MaquinaCreate(BaseModel):
    id_hardware: Optional[str] = None
    nome: str
    cliente_id: Optional[int] = None
    localizacao: Optional[str] = None


class MaquinaUpdate(BaseModel):
    nome: str
    cliente_id: Optional[int] = None
    localizacao: Optional[str] = None


class MaquinaOut(BaseModel):
    id_hardware: str
    cliente_id: Optional[int] = None
    cliente_nome: Optional[str] = None
    nome: Optional[str] = None
    localizacao: Optional[str] = None
    ultimo_sinal: Optional[datetime] = None
    ultimo_pagamento_em: Optional[datetime] = None
    ultimo_teste_em: Optional[datetime] = None
    ultima_saida_em: Optional[datetime] = None
    ultima_atividade_em: Optional[datetime] = None
    status_online: bool = False
    status_operacional: str = "offline"
    faturamento: float = 0.0

    class Config:
        from_attributes = True

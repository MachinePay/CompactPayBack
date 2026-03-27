from datetime import datetime

from pydantic import BaseModel


class FechamentoMaquinaOut(BaseModel):
    id: int
    maquina_id: str
    periodo_inicio: datetime
    periodo_fim: datetime
    total_pagamentos: float
    total_digital: float
    total_fisico: float
    quantidade_pagamentos: int
    quantidade_testes: int
    quantidade_saidas: int
    criado_por_email: str
    created_at: datetime

    class Config:
        from_attributes = True

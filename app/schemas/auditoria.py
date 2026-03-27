from datetime import datetime

from pydantic import BaseModel


class AuditoriaOperacaoOut(BaseModel):
    id: int
    maquina_id: str
    acao: str
    descricao: str
    executado_por_email: str
    created_at: datetime

    class Config:
        from_attributes = True

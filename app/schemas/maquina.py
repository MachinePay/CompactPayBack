from pydantic import BaseModel
from typing import Optional

class MaquinaBase(BaseModel):
    id_hardware: str
    cliente_id: str
    localizacao: str
    status_online: bool
    versao_firmware: str

class MaquinaOut(MaquinaBase):
    class Config:
        from_attributes = True

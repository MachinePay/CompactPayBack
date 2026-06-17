from datetime import datetime
from typing import Optional

from pydantic import BaseModel, HttpUrl


class FirmwareVersionBase(BaseModel):
    nome: str
    url_bin: HttpUrl
    observacao: Optional[str] = None
    ativo: bool = True


class FirmwareVersionCreate(FirmwareVersionBase):
    pass


class FirmwareVersionUpdate(BaseModel):
    nome: Optional[str] = None
    url_bin: Optional[HttpUrl] = None
    observacao: Optional[str] = None
    ativo: Optional[bool] = None


class FirmwareVersionOut(BaseModel):
    id: int
    nome: str
    url_bin: str
    observacao: Optional[str] = None
    ativo: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

from pydantic import BaseModel

class MaquinaBase(BaseModel):
    id_unico: str
    nome: str
    faturamento_total: float
    status_online: bool

class MaquinaCreate(BaseModel):
    id_unico: str
    nome: str

class MaquinaOut(MaquinaBase):
    class Config:
        orm_mode = True

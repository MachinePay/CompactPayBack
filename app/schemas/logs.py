from pydantic import BaseModel
from datetime import datetime

class LogBase(BaseModel):
    message: str
    level: str
    created_at: datetime

class LogCreate(BaseModel):
    message: str
    level: str

class LogOut(LogBase):
    class Config:
        orm_mode = True

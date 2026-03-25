from sqlalchemy import Column, Integer, String, Float, Boolean
from app.db.base import Base

class Maquinas(Base):
    __tablename__ = "maquinas"
    id_unico = Column(String, primary_key=True, index=True)
    nome = Column(String, nullable=False)
    faturamento_total = Column(Float, default=0.0)
    status_online = Column(Boolean, default=False)

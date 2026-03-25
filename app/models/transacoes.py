from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, Enum
from sqlalchemy.orm import relationship
from app.db.base import Base
import enum
from datetime import datetime

class TipoTransacao(str, enum.Enum):
    IN = "IN"
    OUT = "OUT"

class Transacoes(Base):
    __tablename__ = "transacoes"
    id = Column(Integer, primary_key=True, index=True)
    machine_id = Column(String, ForeignKey("maquinas.id_unico"), nullable=False)
    valor = Column(Float, nullable=False)
    tipo = Column(Enum(TipoTransacao), nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

    maquina = relationship("Maquinas")

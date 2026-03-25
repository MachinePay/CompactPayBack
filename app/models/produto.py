from sqlalchemy import Column, Integer, String, Float, ForeignKey
from sqlalchemy.orm import relationship
from app.db.base import Base

class Produto(Base):
    __tablename__ = "produtos"
    id = Column(Integer, primary_key=True)
    nome = Column(String, nullable=False)
    valor = Column(Float, nullable=False)
    maquina_id = Column(String, ForeignKey("maquinas.id_hardware"))
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    maquina = relationship("Maquina")
    usuario = relationship("Usuario")

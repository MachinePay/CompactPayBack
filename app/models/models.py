import enum
from sqlalchemy import Column, String, Integer, ForeignKey, DateTime, Float, Boolean, Enum
from sqlalchemy.orm import relationship
import datetime
from app.db.base import Base

class UserRole(str, enum.Enum):
    admin = "admin"
    cliente = "cliente"

class EventoTipo(str, enum.Enum):
    in_flux = "IN"   # Dinheiro entrando
    out_flux = "OUT" # Pelúcia saindo

class MetodoPagamento(str, enum.Enum):
    fisico = "FISICO"   # Moeda/Nota física na máquina
    digital = "DIGITAL" # Pix/Cartão via CompactPay (Mercado Pago)

class Cliente(Base):
    __tablename__ = "clientes"
    id = Column(Integer, primary_key=True)
    nome_empresa = Column(String, nullable=False)
    email_contato = Column(String, nullable=False, unique=True)
    api_key = Column(String, nullable=False, unique=True)
    maquinas = relationship("Maquina", back_populates="dono")

class Usuario(Base):
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(Enum(UserRole), default=UserRole.cliente)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=True)

from sqlalchemy import DateTime
import datetime

class Maquina(Base):
    __tablename__ = "maquinas"
    id_hardware = Column(String, primary_key=True) # ID do WiFiManager
    cliente_id = Column(Integer, ForeignKey("clientes.id"))
    nome_local = Column(String)
    ultimo_sinal = Column(DateTime, default=datetime.datetime.utcnow)
    dono = relationship("Cliente", back_populates="maquinas")
    transacoes = relationship("Transacao", back_populates="maquina")

class Transacao(Base):
    __tablename__ = "transacoes"
    id = Column(Integer, primary_key=True)
    maquina_id = Column(String, ForeignKey("maquinas.id_hardware"))
    tipo = Column(Enum(EventoTipo))
    metodo = Column(Enum(MetodoPagamento))
    valor = Column(Float, default=1.0)
    data_hora = Column(DateTime, default=datetime.datetime.utcnow)
    maquina = relationship("Maquina", back_populates="transacoes")

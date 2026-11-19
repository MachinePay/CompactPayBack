import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String

from app.db.base import Base

# Valor cobrado por maquina quando o cliente ainda nao tem nada cadastrado.
VALOR_PADRAO_MENSALIDADE = 40.0


class MensalidadeCliente(Base):
    """Valor padrao da mensalidade por maquina de um cliente (R$ 40 se nao
    houver linha). Cobre as maquinas que nao estao em nenhuma faixa."""
    __tablename__ = "mensalidades_clientes"
    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), unique=True, index=True, nullable=False)
    valor_padrao = Column(Float, nullable=False, default=VALOR_PADRAO_MENSALIDADE)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class MensalidadeFaixa(Base):
    """Valor diferente do padrao para N maquinas do cliente
    (ex.: 3 maquinas a R$ 30, 2 maquinas a R$ 60)."""
    __tablename__ = "mensalidades_faixas"
    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), index=True, nullable=False)
    valor = Column(Float, nullable=False)
    quantidade_maquinas = Column(Integer, nullable=False)
    descricao = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

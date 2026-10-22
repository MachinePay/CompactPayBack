import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String

from app.db.base import Base


class TerminalSmartApp(Base):
    """Maquininha Point Smart rodando o app CompactPay (android-point/), banco
    "mercado_pago_apk". O app se autentica com a chave no header
    X-Terminal-Key; aqui fica so o hash. A chave so enxerga maquinas do
    proprio cliente."""
    __tablename__ = "terminais_smartapp"
    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), index=True, nullable=False)
    nome = Column(String, nullable=False)
    chave_prefixo = Column(String, nullable=False)
    chave_hash = Column(String, unique=True, index=True, nullable=False)
    ativo = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    ultimo_uso_em = Column(DateTime, nullable=True)

import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String

from app.db.base import Base


class TerminalConfrapag(Base):
    """Maquininha PAX (A910/A920/A960/P2) rodando o app CompactPay com o SDK
    ConfraPag (android-confrapag/), banco "confrapag_sdk".

    O app se autentica com a chave no header X-Terminal-Key; aqui fica so o
    hash. O CNPJ e' o do estabelecimento cadastrado na ConfraPag: o SDK nao tem
    token, a identificacao do terminal e' o proprio CNPJ."""
    __tablename__ = "terminais_confrapag"
    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), index=True, nullable=False)
    nome = Column(String, nullable=False)
    cnpj = Column(String, nullable=False)
    chave_prefixo = Column(String, nullable=False)
    chave_hash = Column(String, unique=True, index=True, nullable=False)
    ativo = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    ultimo_uso_em = Column(DateTime, nullable=True)

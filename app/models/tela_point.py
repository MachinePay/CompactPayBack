import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String

from app.db.base import Base


class TelaPoint(Base):
    """Tela touch (ESP32) de uma maquina com banco "mercado_pago_point".

    A tela mostra os valores, o cliente escolhe um e o backend manda a cobranca
    para a Point (modo integrado/PDV) pela API de Orders do Mercado Pago. A tela
    se autentica com a chave no header X-Tela-Key; aqui fica so o hash. Cada
    tela enxerga so a propria maquina, e cada maquina tem no maximo uma tela ativa.
    """
    __tablename__ = "telas_point"
    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), index=True, nullable=False)
    maquina_id = Column(String, index=True, nullable=False)  # Maquina.id_hardware
    nome = Column(String, nullable=False)
    point_terminal_id = Column(String, nullable=False)  # id da Point no MP (ex.: NEWLAND_N950__N950NCB123456)
    chave_prefixo = Column(String, nullable=False)
    chave_hash = Column(String, unique=True, index=True, nullable=False)
    ativo = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    ultimo_uso_em = Column(DateTime, nullable=True)

"""Pontos: agrupamento OPCIONAL das maquinas de um cliente (so visual, para
organizar a lista). Nao confundir com Maquina.nome_local ("Nome do ponto" da
maquina), que continua igual. A maquina aponta para o ponto por
Maquina.ponto_id (nullable, sem FK no banco: excluir um ponto zera esse campo
em app/api/v1/endpoints/pontos_grupos.py)."""

import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.db.base import Base


class PontoGrupo(Base):
    __tablename__ = "pontos_grupos"
    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), index=True, nullable=False)
    nome = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

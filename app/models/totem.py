"""Totem vending: maquina com tablet + agente local (projeto VendingMachine).

Diferente das maquinas de pulso (ESP32 via MQTT), o totem tem uma placa propria
ligada no tablet e um agente que decide tudo localmente (offline-first). O
CompactPay aqui so:
  - guarda o catalogo (produtos por baia, fotos) e a maquininha da maquina;
  - entrega a configuracao ao agente (GET /totem/{id}/config);
  - recebe os eventos de venda e registra Transacao/VendaPagamento da maquina,
    para o totem aparecer no dashboard, relatorios e saude como as outras.

Tabelas so novas (create_all). Nenhuma coluna existente e' alterada.
"""
import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import deferred

from app.db.base import Base


class TotemVending(Base):
    """Uma por maquina. O agente se autentica com o token (aqui so o hash);
    o codigo de vinculo e' de uso unico e expira."""
    __tablename__ = "totens_vending"
    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), index=True, nullable=False)
    maquina_id = Column(String, unique=True, index=True, nullable=False)  # Maquina.id_hardware
    ativo = Column(Boolean, default=True, nullable=False)

    token_prefixo = Column(String, nullable=True)
    token_hash = Column(String, unique=True, index=True, nullable=True)
    codigo_hash = Column(String, index=True, nullable=True)
    codigo_expira_em = Column(DateTime, nullable=True)

    # Maquininha do totem: off | mercadopago | sumup. Credenciais vem do Cliente
    # (mp_access_token / sumup_api_key), como nas outras maquinas.
    modo_cartao = Column(String, default="off", nullable=False)
    mp_terminal_id = Column(String, nullable=True)
    sumup_reader_id = Column(String, nullable=True)

    config_versao = Column(Integer, default=1, nullable=False)
    vinculado_em = Column(DateTime, nullable=True)
    ultimo_contato = Column(DateTime, nullable=True)
    versao_agente = Column(String, nullable=True)
    versao_placa = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)


class TotemProduto(Base):
    """Produto de cada baia (0 a 8). Preco sempre em centavos."""
    __tablename__ = "totens_produtos"
    __table_args__ = (UniqueConstraint("maquina_id", "slot", name="uq_totem_produto_slot"),)
    id = Column(Integer, primary_key=True)
    maquina_id = Column(String, index=True, nullable=False)
    slot = Column(Integer, nullable=False)
    nome = Column(String, nullable=False)
    preco_centavos = Column(Integer, nullable=False, default=500)
    imagem_hash = Column(String, nullable=True)
    ativo = Column(Boolean, default=True, nullable=False)
    estoque = Column(Integer, nullable=True)
    estoque_minimo = Column(Integer, nullable=True)
    ordem = Column(Integer, default=0, nullable=False)
    atualizado_em = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    atualizado_por = Column(String, nullable=True)


class TotemMidia(Base):
    """Foto de produto em WebP, identificada pelo sha256 do conteudo."""
    __tablename__ = "totens_midias"
    hash = Column(String, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), index=True, nullable=True)
    tamanho = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    conteudo = deferred(Column(LargeBinary, nullable=False))


class TotemVideo(Base):
    """Video da tela inicial do totem (MP4/WebM como foi enviado: o tablet toca direto
    no navegador). Um por maquina; o agente baixa pelo hash quando muda."""
    __tablename__ = "totens_videos"
    id = Column(Integer, primary_key=True)
    maquina_id = Column(String, unique=True, index=True, nullable=False)
    hash = Column(String, index=True, nullable=False)
    mime = Column(String, nullable=False)
    tamanho = Column(Integer, nullable=False)
    nome_original = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    conteudo = deferred(Column(LargeBinary, nullable=False))


class TotemEvento(Base):
    """Evento recebido do agente (id UUID v7 gerado na maquina): garante que
    reenvio nao duplica venda."""
    __tablename__ = "totens_eventos"
    id = Column(String, primary_key=True)
    maquina_id = Column(String, index=True, nullable=False)
    tipo = Column(String, nullable=False)
    payload = Column(Text, nullable=False)
    criado_em = Column(DateTime, nullable=True)
    recebido_em = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

"""Aviso de maquina offline pelo WhatsApp (Evolution API). Ver
app/services/whatsapp_alertas.py."""

import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.db.base import Base


class WhatsappConfig(Base):
    """Linha unica (id=1) com a conexao da Evolution API e os textos."""
    __tablename__ = "whatsapp_config"
    id = Column(Integer, primary_key=True)
    ativo = Column(Boolean, default=False, nullable=False)
    evolution_url = Column(String, nullable=True)
    # Criptografada (mesmo Fernet das senhas de Wi-Fi).
    api_key_cript = Column(String, nullable=True)
    instancia = Column(String, nullable=True)
    minutos_offline = Column(Integer, default=60, nullable=False)
    mensagem_offline = Column(Text, nullable=True)
    enviar_voltou = Column(Boolean, default=False, nullable=False)
    mensagem_voltou = Column(Text, nullable=True)
    # Quando foi ativado: maquinas que ja estavam offline antes disso nao
    # disparam (evita uma rajada de avisos antigos ao ligar o envio).
    ativado_em = Column(DateTime, nullable=True)
    updated_by = Column(String, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class WhatsappCliente(Base):
    """Clientes escolhidos para receber o aviso. Sem linha (ou ativo=False)
    o cliente nao recebe nada. Envia para o telefone do perfil."""
    __tablename__ = "whatsapp_clientes"
    cliente_id = Column(Integer, ForeignKey("clientes.id"), primary_key=True)
    ativo = Column(Boolean, default=False, nullable=False)
    # O numero e' sempre o telefone do perfil (ver telefones_do_cliente).
    ativado_em = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class WhatsappEnvio(Base):
    """Cada mensagem enviada (ou que falhou). Tambem serve para nao repetir o
    aviso: um por numero a cada queda (maquina + offline_desde)."""
    __tablename__ = "whatsapp_envios"
    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), index=True, nullable=True)
    maquina_id = Column(String, index=True, nullable=True)
    # offline | voltou | teste
    tipo = Column(String, nullable=False)
    telefone = Column(String, nullable=False)
    mensagem = Column(Text, nullable=False)
    # enviado | erro
    status = Column(String, nullable=False)
    erro = Column(String, nullable=True)
    offline_desde = Column(DateTime, nullable=True)
    # Aviso de offline: quando a maquina voltou (e o "voltou" foi tratado).
    resolvido_em = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

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
    telefone = Column(String, nullable=True)
    cpf = Column(String, nullable=True)
    cnpj = Column(String, nullable=True)
    endereco_rua = Column(String, nullable=True)
    endereco_numero = Column(String, nullable=True)
    endereco_cidade = Column(String, nullable=True)
    endereco_estado = Column(String, nullable=True)
    endereco_latitude = Column(Float, nullable=True)
    endereco_longitude = Column(Float, nullable=True)
    cliente_mercado_pago = Column(Boolean, nullable=True)
    cliente_pagbank = Column(Boolean, nullable=True)
    cliente_s6pay = Column(Boolean, nullable=True)
    mp_public_key = Column(String, nullable=True)
    mp_access_token = Column(String, nullable=True)
    mp_client_id = Column(String, nullable=True)
    mp_client_secret = Column(String, nullable=True)
    mp_user_id = Column(String, nullable=True)
    mp_refresh_token = Column(String, nullable=True)
    mp_token_expires_at = Column(DateTime, nullable=True)
    mp_live_mode = Column(Boolean, nullable=True)
    mp_scope = Column(String, nullable=True)
    mp_pos_category = Column(Integer, nullable=True)
    mp_store_id = Column(String, nullable=True)
    mp_store_external_id = Column(String, nullable=True)
    maquinas = relationship("Maquina", back_populates="dono")

class Usuario(Base):
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True)
    nome = Column(String, nullable=True)
    telefone = Column(String, nullable=True)
    cpf = Column(String, nullable=True)
    cnpj = Column(String, nullable=True)
    endereco_rua = Column(String, nullable=True)
    endereco_numero = Column(String, nullable=True)
    endereco_cidade = Column(String, nullable=True)
    endereco_estado = Column(String, nullable=True)
    endereco_latitude = Column(Float, nullable=True)
    endereco_longitude = Column(Float, nullable=True)
    cliente_mercado_pago = Column(Boolean, nullable=True)
    cliente_pagbank = Column(Boolean, nullable=True)
    cliente_s6pay = Column(Boolean, nullable=True)
    mp_public_key = Column(String, nullable=True)
    mp_access_token = Column(String, nullable=True)
    mp_client_id = Column(String, nullable=True)
    mp_client_secret = Column(String, nullable=True)
    mp_user_id = Column(String, nullable=True)
    mp_refresh_token = Column(String, nullable=True)
    mp_token_expires_at = Column(DateTime, nullable=True)
    mp_live_mode = Column(Boolean, nullable=True)
    mp_scope = Column(String, nullable=True)
    mp_pos_category = Column(Integer, nullable=True)
    mp_store_id = Column(String, nullable=True)
    mp_store_external_id = Column(String, nullable=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(Enum(UserRole), default=UserRole.cliente)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=True)
    cliente = relationship("Cliente")

from sqlalchemy import DateTime
import datetime

class Maquina(Base):
    __tablename__ = "maquinas"
    id_hardware = Column(String, primary_key=True) # ID do WiFiManager
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=True)
    banco_pagamento = Column(String, nullable=True)
    nome_local = Column(String)
    localizacao = Column(String, nullable=True)
    mp_store_id = Column(String, nullable=True)
    mp_store_external_id = Column(String, nullable=True)
    mp_pos_id = Column(String, nullable=True)
    mp_pos_external_id = Column(String, nullable=True)
    mp_qr_image = Column(String, nullable=True)
    ultimo_sinal = Column(DateTime, nullable=True)
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


class HistoricoOperacao(Base):
    __tablename__ = "historico_operacoes"
    id = Column(Integer, primary_key=True)
    maquina_id = Column(String, ForeignKey("maquinas.id_hardware"), index=True, nullable=False)
    categoria = Column(String, nullable=False, index=True)
    descricao = Column(String, nullable=False)
    valor = Column(Float, nullable=True)
    provider = Column(String, nullable=True)
    provider_payment_id = Column(String, nullable=True)
    payment_type = Column(String, nullable=True)
    card_brand = Column(String, nullable=True)
    bank_name = Column(String, nullable=True)
    pulse_status = Column(String, nullable=True)
    refunded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)


class FechamentoMaquina(Base):
    __tablename__ = "fechamentos_maquina"
    id = Column(Integer, primary_key=True)
    maquina_id = Column(String, ForeignKey("maquinas.id_hardware"), index=True, nullable=False)
    periodo_inicio = Column(DateTime, nullable=False, index=True)
    periodo_fim = Column(DateTime, nullable=False, index=True)
    total_pagamentos = Column(Float, default=0.0, nullable=False)
    total_digital = Column(Float, default=0.0, nullable=False)
    total_fisico = Column(Float, default=0.0, nullable=False)
    quantidade_pagamentos = Column(Integer, default=0, nullable=False)
    quantidade_testes = Column(Integer, default=0, nullable=False)
    quantidade_saidas = Column(Integer, default=0, nullable=False)
    criado_por_email = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)


class AuditoriaOperacao(Base):
    __tablename__ = "auditoria_operacoes"
    id = Column(Integer, primary_key=True)
    maquina_id = Column(String, ForeignKey("maquinas.id_hardware"), index=True, nullable=False)
    acao = Column(String, nullable=False, index=True)
    descricao = Column(String, nullable=False)
    executado_por_email = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)


class AuditoriaSistema(Base):
    __tablename__ = "auditoria_sistema"
    id = Column(Integer, primary_key=True)
    entidade_tipo = Column(String, nullable=False, index=True)
    entidade_id = Column(String, nullable=True, index=True)
    acao = Column(String, nullable=False, index=True)
    descricao = Column(String, nullable=False)
    executado_por_email = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)


class EscutaTerminal(Base):
    __tablename__ = "escutas_terminal"
    terminal_id = Column(String, primary_key=True)
    maquina_id = Column(String, ForeignKey("maquinas.id_hardware"), index=True, nullable=False)
    ativo = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

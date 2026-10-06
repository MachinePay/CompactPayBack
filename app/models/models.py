import enum
from sqlalchemy import Column, String, Integer, ForeignKey, DateTime, Float, Boolean, Enum, Text, LargeBinary
from sqlalchemy.orm import deferred, relationship
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
    cliente_token_play = Column(Boolean, nullable=True)
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
    # SumUp usa API key fixa por cliente (preenchida manualmente), sem OAuth e
    # sem conceito de loja/caixa - so precisa do merchant_code pra montar a URL
    # dos readers (maquininhas) ja pareadas na conta SumUp desse cliente.
    cliente_sumup = Column(Boolean, nullable=True)
    sumup_api_key = Column(String, nullable=True)
    sumup_merchant_code = Column(String, nullable=True)
    # Marca d'agua do polling (sumup_poller.py) - ate onde ja consultamos o
    # historico de transacoes desse cliente, pra so buscar o que mudou desde
    # a ultima rodada (changes_since) em vez de reprocessar tudo toda vez.
    sumup_last_sync_at = Column(DateTime, nullable=True)
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
    cliente_token_play = Column(Boolean, nullable=True)
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
    cliente_sumup = Column(Boolean, nullable=True)
    sumup_api_key = Column(String, nullable=True)
    sumup_merchant_code = Column(String, nullable=True)
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
    # Qual reader (maquininha) da conta SumUp do cliente esta vinculado a esta
    # maquina especifica - ao contrario do MP, o SumUp nao cria loja/caixa
    # automaticamente; o reader ja precisa estar pareado no app SumUp e so
    # escolhido aqui (ver listar_sumup_readers em clientes.py).
    sumup_reader_id = Column(String, nullable=True)
    # Serial do reader fisico (transaction_data.card_reader.code no recibo da
    # SumUp) - usado pra identificar standalone sem precisar que o reader
    # esteja "Cloud-paired" (sumup_reader_id so existe pra isso). Descoberto
    # fazendo um pagamento teste na maquininha e lendo o codigo no log.
    sumup_device_code = Column(String, nullable=True)
    ultimo_sinal = Column(DateTime, nullable=True)
    wifi_rssi = Column(Integer, nullable=True)
    wifi_quality = Column(Integer, nullable=True)
    firmware_version = Column(String, nullable=True)
    firmware_target_version = Column(String, nullable=True)
    firmware_updated_at = Column(DateTime, nullable=True)
    firmware_update_status = Column(String, nullable=True)
    firmware_update_command_id = Column(String, nullable=True)
    firmware_update_url = Column(String, nullable=True)
    firmware_update_requested_at = Column(DateTime, nullable=True)
    firmware_update_started_at = Column(DateTime, nullable=True)
    firmware_update_finished_at = Column(DateTime, nullable=True)
    firmware_update_progress = Column(Integer, nullable=True)
    firmware_update_error = Column(String, nullable=True)
    firmware_last_good_version = Column(String, nullable=True)
    uptime_seconds = Column(Integer, nullable=True)
    free_heap_bytes = Column(Integer, nullable=True)
    last_reset_reason = Column(String, nullable=True)
    wifi_reconnect_count = Column(Integer, nullable=True)
    mqtt_reconnect_count = Column(Integer, nullable=True)
    short_pulse_count = Column(Integer, nullable=True)
    wifi_disconnect_reason = Column(Integer, nullable=True)
    wifi_disconnect_count = Column(Integer, nullable=True)
    last_forced_restart_reason = Column(String, nullable=True)
    last_forced_restart_at = Column(DateTime, nullable=True)
    ignorar_saida_pos_credito = Column(Boolean, nullable=True, default=True)
    credito_liberado_em = Column(DateTime, nullable=True)
    # Tempos (ms) da escada de reconexao de Wi-Fi da placa - NULL usa o padrao
    # do firmware (ver WIFI_RECONNECT_HARD_RESET_AFTER_MS_DEFAULT/
    # WIFI_RECONNECT_FULL_RESTART_AFTER_MS_DEFAULT no .ino); ajustavel por
    # maquina via POST /maquinas/{id}/config-reconexao para locais com
    # roteador instavel que precisam de um tempo diferente do padrao.
    wifi_hard_reset_ms = Column(Integer, nullable=True)
    wifi_full_restart_ms = Column(Integer, nullable=True)
    # Espelham 1:1 os campos do portal fisico de configuracao da placa (menos
    # SSID/senha de Wi-Fi, que continuam so pelo portal) - NULL aqui so
    # significa "nunca foi ajustado por aqui", a placa continua com o que ja
    # tinha gravado; ajustaveis remotamente via POST
    # /maquinas/{id}/config-dispositivo, sem precisar abrir o portal fisico.
    pulse_coin = Column(String, nullable=True)
    pulse_out = Column(String, nullable=True)
    pulse_credit = Column(String, nullable=True)
    pulse_value = Column(String, nullable=True)
    pulse_quantity = Column(String, nullable=True)
    coin_debounce_us = Column(String, nullable=True)
    coin_release_ms = Column(String, nullable=True)
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


class VendaPagamento(Base):
    __tablename__ = "vendas_pagamentos"
    id = Column(Integer, primary_key=True)
    maquina_id = Column(String, ForeignKey("maquinas.id_hardware"), index=True, nullable=False)
    transacao_id = Column(Integer, ForeignKey("transacoes.id"), nullable=True, index=True)
    historico_id = Column(Integer, ForeignKey("historico_operacoes.id"), nullable=True, index=True)
    origem = Column(String, nullable=False, index=True)
    provider = Column(String, nullable=True, index=True)
    provider_payment_id = Column(String, nullable=True, index=True)
    tipo_pagamento = Column(String, nullable=True)
    bandeira_cartao = Column(String, nullable=True)
    banco = Column(String, nullable=True)
    valor_bruto = Column(Float, default=0.0, nullable=False)
    taxa = Column(Float, nullable=True)
    valor_liquido = Column(Float, default=0.0, nullable=False)
    status_pulso = Column(String, nullable=True, index=True)
    command_id = Column(String, nullable=True, index=True)
    conta_faturamento = Column(Boolean, default=True, nullable=False)
    conta_ticket_medio = Column(Boolean, default=True, nullable=False)
    is_teste = Column(Boolean, default=False, nullable=False)
    is_manual = Column(Boolean, default=False, nullable=False)
    refunded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False, index=True)


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
    card_last_four = Column(String, nullable=True)
    bank_name = Column(String, nullable=True)
    pulse_status = Column(String, nullable=True)
    command_id = Column(String, nullable=True, index=True)
    refunded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)


class SumupCheckout(Base):
    # Mapeia checkout_id -> maquina/valor no momento da criacao da cobranca.
    # Necessario porque o webhook do SumUp manda SO {event_type, id} (o id do
    # checkout) - sem machine_id, reader_id nem valor embutidos (diferente do
    # Mercado Pago, que usa external_reference pra isso). Sem essa tabela nao
    # teria como saber qual maquina liberar credito quando o webhook chegar.
    __tablename__ = "sumup_checkouts"
    checkout_id = Column(String, primary_key=True)
    maquina_id = Column(String, ForeignKey("maquinas.id_hardware"), index=True, nullable=False)
    reader_id = Column(String, nullable=False)
    valor = Column(Float, nullable=False)
    processado = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)


class SumupTransacaoPendente(Base):
    # Pagamento SumUp (detectado pelo polling, feito direto na maquininha)
    # cujo reader nao bateu com nenhuma maquina com confianca - cliente tem
    # mais de uma maquina na conta SumUp e nao foi possivel identificar qual
    # delas recebeu. Fica visivel no painel de alertas (maquinas_relatorio.py)
    # ate um admin vincular manualmente (ou ignorar) via
    # /pagamentos/sumup/pendencias/{id}/resolver|ignorar.
    __tablename__ = "sumup_transacoes_pendentes"
    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), index=True, nullable=False)
    transaction_id = Column(String, unique=True, index=True, nullable=False)
    valor = Column(Float, nullable=False)
    device_identifier = Column(String, nullable=True)
    raw_payload = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    resolvido = Column(Boolean, default=False, nullable=False)
    maquina_id = Column(String, ForeignKey("maquinas.id_hardware"), nullable=True)
    resolvido_em = Column(DateTime, nullable=True)
    resolvido_por = Column(String, nullable=True)


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


class ComandoMaquina(Base):
    __tablename__ = "comandos_maquina"
    id = Column(Integer, primary_key=True)
    command_id = Column(String, unique=True, index=True, nullable=False)
    maquina_id = Column(String, ForeignKey("maquinas.id_hardware"), index=True, nullable=False)
    tipo = Column(String, nullable=False, index=True)
    topic = Column(String, nullable=False)
    payload = Column(String, nullable=False)
    status = Column(String, default="pendente", nullable=False, index=True)
    detalhe_status = Column(String, nullable=True)
    tentativas = Column(Integer, default=0, nullable=False)
    max_tentativas = Column(Integer, default=3, nullable=False)
    ultimo_erro = Column(String, nullable=True)
    next_retry_at = Column(DateTime, nullable=True, index=True)
    sent_at = Column(DateTime, nullable=True)
    ack_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)


class AlertaNotificacao(Base):
    __tablename__ = "alertas_notificacoes"
    id = Column(Integer, primary_key=True)
    alerta_key = Column(String, unique=True, index=True, nullable=False)
    maquina_id = Column(String, ForeignKey("maquinas.id_hardware"), index=True, nullable=False)
    tipo = Column(String, nullable=False, index=True)
    severidade = Column(String, nullable=False)
    primeira_notificacao_em = Column(DateTime, nullable=False)
    ultima_notificacao_em = Column(DateTime, nullable=False)
    resolvido_em = Column(DateTime, nullable=True, index=True)


class FirmwareVersion(Base):
    __tablename__ = "firmware_versions"
    id = Column(Integer, primary_key=True)
    nome = Column(String, nullable=False)
    url_bin = Column(String, nullable=False)
    observacao = Column(String, nullable=True)
    ativo = Column(Boolean, default=True, nullable=False)
    # O .bin enviado pelo painel fica no proprio banco: o disco do Render e'
    # temporario (apagado a cada deploy/reinicio) e o arquivo sumia, fazendo a
    # placa receber 404 no OTA. deferred() evita carregar ~1 MB por linha so
    # para listar as versoes.
    arquivo_nome = Column(String, nullable=True, index=True)
    arquivo_tamanho = Column(Integer, nullable=True)
    arquivo_bin = deferred(Column(LargeBinary, nullable=True))
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)


class EscutaTerminal(Base):
    __tablename__ = "escutas_terminal"
    terminal_id = Column(String, primary_key=True)
    maquina_id = Column(String, ForeignKey("maquinas.id_hardware"), index=True, nullable=False)
    ativo = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

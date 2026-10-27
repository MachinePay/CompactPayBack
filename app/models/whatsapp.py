"""Avisos pelo WhatsApp (Evolution API): maquina offline, lembrete, voltou e
resumo diario. Ver app/services/whatsapp_alertas.py."""

import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.db.base import Base


class WhatsappConfig(Base):
    """Linha unica (id=1) com a conexao da Evolution API, tempos e textos."""
    __tablename__ = "whatsapp_config"
    id = Column(Integer, primary_key=True)
    ativo = Column(Boolean, default=False, nullable=False)
    evolution_url = Column(String, nullable=True)
    # Criptografada (mesmo Fernet das senhas de Wi-Fi).
    api_key_cript = Column(String, nullable=True)
    instancia = Column(String, nullable=True)
    minutos_offline = Column(Integer, default=60, nullable=False)
    mensagem_offline = Column(Text, nullable=True)
    # Sem uso: o aviso de "voltou" agora e' escolhido por cliente.
    enviar_voltou = Column(Boolean, default=False, nullable=False)
    mensagem_voltou = Column(Text, nullable=True)
    # Lembrete enquanto continua offline: a cada X horas (0 = nunca).
    lembrete_horas = Column(Integer, default=24, nullable=False)
    mensagem_lembrete = Column(Text, nullable=True)
    # Resumo diario: horario padrao (HH:MM, Brasilia); o cliente pode ter outro.
    resumo_horario = Column(String, default="22:00", nullable=False)
    mensagem_resumo = Column(Text, nullable=True)
    # Quando foi ativado: maquinas que ja estavam offline antes disso nao
    # disparam (evita uma rajada de avisos antigos ao ligar o envio).
    ativado_em = Column(DateTime, nullable=True)
    # Trava para so um processo do backend enviar por vez (ver _pegar_trava).
    worker_lock_id = Column(String, nullable=True)
    worker_lock_ate = Column(DateTime, nullable=True)
    # Diagnostico do ultimo ciclo do envio automatico (mostrado na tela).
    ultimo_ciclo_em = Column(DateTime, nullable=True)
    ultimo_ciclo_status = Column(String, nullable=True)
    updated_by = Column(String, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class WhatsappCliente(Base):
    """O que cada cliente recebe. Sem linha o cliente nao recebe nada. O
    numero e' sempre o telefone do perfil (ver telefones_do_cliente)."""
    __tablename__ = "whatsapp_clientes"
    cliente_id = Column(Integer, ForeignKey("clientes.id"), primary_key=True)
    # Aviso de maquina offline.
    ativo = Column(Boolean, default=False, nullable=False)
    recebe_lembrete = Column(Boolean, default=False, nullable=False)
    recebe_voltou = Column(Boolean, default=False, nullable=False)
    resumo_diario = Column(Boolean, default=False, nullable=False)
    # HH:MM; vazio = horario padrao da configuracao.
    resumo_horario = Column(String, nullable=True)
    # Ultimo dia (AAAA-MM-DD, Brasilia) em que o resumo saiu.
    resumo_ultimo_dia = Column(String, nullable=True)
    ativado_em = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class WhatsappOfflineAviso(Base):
    """Uma queda avisada (maquina + ultimo_sinal). Controla o lembrete e o
    "voltou", e impede avisar a mesma queda duas vezes."""
    __tablename__ = "whatsapp_offline_avisos"
    __table_args__ = (UniqueConstraint("maquina_id", "offline_desde", name="uq_whatsapp_aviso_queda"),)
    id = Column(Integer, primary_key=True)
    maquina_id = Column(String, index=True, nullable=False)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), index=True, nullable=True)
    offline_desde = Column(DateTime, nullable=False)
    # Inicio considerado (depois da abertura do ponto, se estava fechado).
    inicio_offline = Column(DateTime, nullable=True)
    avisado_em = Column(DateTime, nullable=False)
    ultimo_lembrete_em = Column(DateTime, nullable=True)
    lembretes = Column(Integer, default=0, nullable=False)
    resolvido_em = Column(DateTime, nullable=True)


class WhatsappEnvio(Base):
    """Historico de cada mensagem enviada (ou que falhou)."""
    __tablename__ = "whatsapp_envios"
    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), index=True, nullable=True)
    # IDs das maquinas da mensagem, separados por virgula.
    maquina_id = Column(String, index=True, nullable=True)
    # offline | lembrete | voltou | resumo | teste
    tipo = Column(String, nullable=False)
    telefone = Column(String, nullable=False)
    mensagem = Column(Text, nullable=False)
    # enviado | erro
    status = Column(String, nullable=False)
    erro = Column(String, nullable=True)
    # Sem uso desde que o controle da queda foi para WhatsappOfflineAviso.
    offline_desde = Column(DateTime, nullable=True)
    resolvido_em = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

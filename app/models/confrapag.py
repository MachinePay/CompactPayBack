import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String

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
    # Chave de teste (marcada no painel): aceita "pagamento de teste" do app de teste,
    # que libera o pulso sem cartao. Venda fica como teste, fora do faturamento.
    modo_teste = Column(Boolean, default=False, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    ultimo_uso_em = Column(DateTime, nullable=True)


class CobrancaPixConfrapag(Base):
    """Cobranca Pix (Confrapix, Pix dinamico) criada pela maquininha do banco
    "confrapag_sdk". O pulso so sai depois de a cobranca ser reconsultada na
    Confrapix com o token do cliente (o webhook deles nao tem assinatura)."""
    __tablename__ = "cobrancas_pix_confrapag"
    id = Column(Integer, primary_key=True)
    terminal_id = Column(Integer, ForeignKey("terminais_confrapag.id"), index=True, nullable=False)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), index=True, nullable=False)
    maquina_id = Column(String, ForeignKey("maquinas.id_hardware"), index=True, nullable=False)
    referencia = Column(String, nullable=False)
    valor = Column(Float, nullable=False)
    confrapix_id = Column(String, unique=True, index=True, nullable=True)
    txid = Column(String, nullable=True)
    codigo = Column(String, nullable=True)  # copia-e-cola (vira o QR na maquininha)
    status = Column(String, default="processing", nullable=False)
    expira_em = Column(DateTime, nullable=True)
    pago_em = Column(DateTime, nullable=True)
    command_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

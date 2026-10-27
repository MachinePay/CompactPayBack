import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db")
os.environ["START_MQTT_WORKER"] = "false"
os.environ["START_COMMAND_QUEUE_WORKER"] = "false"
os.environ["START_RETENTION_WORKER"] = "false"
os.environ["START_ALERT_NOTIFIER_WORKER"] = "false"
os.environ["START_WHATSAPP_ALERT_WORKER"] = "false"
os.environ.setdefault("SECRET_KEY", "test-secret-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import Cliente, Maquina, Usuario
import app.models.produto  # noqa: F401
from app.models.whatsapp import WhatsappCliente, WhatsappConfig, WhatsappEnvio
from app.services import whatsapp_alertas
from app.services.horario_fechado import salvar_horarios

Base.metadata.create_all(bind=engine)

# 2026-10-26 12:00 em Brasilia (segunda).
AGORA = datetime(2026, 10, 26, 15, 0)


@pytest.fixture
def ambiente(monkeypatch):
    enviados = []
    monkeypatch.setattr(whatsapp_alertas, "estado_conexao", lambda config: "open")
    monkeypatch.setattr(whatsapp_alertas, "enviar_texto", lambda config, numero, texto: enviados.append((numero, texto)))
    monkeypatch.setattr(whatsapp_alertas, "PAUSA_ENTRE_MENSAGENS_SEGUNDOS", 0)

    db = SessionLocal()
    db.query(WhatsappEnvio).delete()
    db.query(WhatsappCliente).delete()
    db.query(WhatsappConfig).delete()
    db.add(
        WhatsappConfig(
            id=1, ativo=True, evolution_url="https://evo.test", api_key_cript="x", instancia="cp",
            minutos_offline=60, enviar_voltou=True, ativado_em=AGORA - timedelta(days=30),
        )
    )
    db.commit()
    yield db, enviados
    db.close()


def _cliente(db, ativo=True, telefone="(11) 98888-7777"):
    sufixo = uuid.uuid4().hex[:8]
    cliente = Cliente(nome_empresa=f"Cli {sufixo}", email_contato=f"{sufixo}@t.com", api_key=sufixo, telefone=telefone)
    db.add(cliente)
    db.flush()
    db.add(WhatsappCliente(cliente_id=cliente.id, ativo=ativo, ativado_em=AGORA - timedelta(days=30)))
    db.commit()
    return cliente


def _maquina(db, cliente, ultimo_sinal, horarios=None):
    maquina = Maquina(id_hardware=f"W{uuid.uuid4().hex[:6]}", nome_local="Grua", cliente_id=cliente.id, ultimo_sinal=ultimo_sinal)
    if horarios:
        salvar_horarios(maquina, horarios)
    db.add(maquina)
    db.commit()
    return maquina


def test_envia_uma_vez_apos_uma_hora(ambiente):
    db, enviados = ambiente
    cliente = _cliente(db)
    _maquina(db, cliente, AGORA - timedelta(minutes=61))
    _maquina(db, cliente, AGORA - timedelta(minutes=30))

    assert whatsapp_alertas.verificar_e_enviar(db, AGORA)["offline"] == 1
    assert enviados[0][0] == "5511988887777"
    assert "1h 1min" in enviados[0][1]
    # Proximo ciclo: mesma queda, nao repete.
    assert whatsapp_alertas.verificar_e_enviar(db, AGORA + timedelta(minutes=1))["offline"] == 0


def test_cliente_nao_escolhido_nao_recebe(ambiente):
    db, enviados = ambiente
    cliente = _cliente(db, ativo=False)
    _maquina(db, cliente, AGORA - timedelta(hours=3))
    whatsapp_alertas.verificar_e_enviar(db, AGORA)
    assert enviados == []


def test_ponto_fechado_nao_avisa_e_conta_da_abertura(ambiente):
    db, enviados = ambiente
    cliente = _cliente(db)
    # Fecha domingo 22:00 ate segunda 11:30; desligada desde domingo 21:00.
    _maquina(db, cliente, datetime(2026, 10, 26, 0, 0), horarios=[{"dia": 6, "inicio": "22:00", "fim": "11:30"}])
    # Segunda 12:00 (30 min depois de abrir): ainda nao.
    whatsapp_alertas.verificar_e_enviar(db, AGORA)
    assert enviados == []
    # Segunda 12:31 (1h01 depois de abrir): avisa.
    whatsapp_alertas.verificar_e_enviar(db, AGORA + timedelta(minutes=61))
    assert len(enviados) == 1


def test_ja_offline_antes_de_ativar_nao_avisa(ambiente):
    db, enviados = ambiente
    cliente = _cliente(db)
    db.get(WhatsappCliente, cliente.id).ativado_em = AGORA
    db.commit()
    _maquina(db, cliente, AGORA - timedelta(hours=5))
    whatsapp_alertas.verificar_e_enviar(db, AGORA)
    assert enviados == []


def test_avisa_quando_volta(ambiente):
    db, enviados = ambiente
    cliente = _cliente(db)
    maquina = _maquina(db, cliente, AGORA - timedelta(hours=2))
    whatsapp_alertas.verificar_e_enviar(db, AGORA)
    maquina.ultimo_sinal = AGORA + timedelta(minutes=5)
    db.commit()
    assert whatsapp_alertas.verificar_e_enviar(db, AGORA + timedelta(minutes=5))["voltou"] == 1
    assert "voltou" in enviados[-1][1]


def test_desconectado_nao_envia_nem_marca(ambiente, monkeypatch):
    db, enviados = ambiente
    monkeypatch.setattr(whatsapp_alertas, "estado_conexao", lambda config: "close")
    cliente = _cliente(db)
    _maquina(db, cliente, AGORA - timedelta(hours=2))
    assert whatsapp_alertas.verificar_e_enviar(db, AGORA)["pulado"] == "desconectado"
    assert db.query(WhatsappEnvio).count() == 0


def test_mensagem_e_telefone():
    assert whatsapp_alertas.normalizar_telefone("011 98888-7777") == "5511988887777"
    assert whatsapp_alertas.normalizar_telefone("123") is None
    texto = whatsapp_alertas.montar_mensagem("Oi {cliente} {chave_solta} {maquina}", {"cliente": "A", "maquina": "B"})
    assert texto == "Oi A {chave_solta} B"


def test_usa_telefone_do_perfil_do_usuario(ambiente):
    db, enviados = ambiente
    cliente = _cliente(db, telefone="(11) 90000-0000")
    db.add(Usuario(email=f"u{cliente.id}@t.com", hashed_password="x", cliente_id=cliente.id, telefone="21 97777-6666"))
    db.commit()
    _maquina(db, cliente, AGORA - timedelta(hours=2))
    whatsapp_alertas.verificar_e_enviar(db, AGORA)
    assert [numero for numero, _ in enviados] == ["5521977776666"]

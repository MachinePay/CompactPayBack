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
from app.models.whatsapp import WhatsappCliente, WhatsappConfig, WhatsappEnvio, WhatsappOfflineAviso
from app.services import whatsapp_alertas
from app.services.horario_fechado import salvar_horarios, salvar_pontuais

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
    for modelo in (WhatsappEnvio, WhatsappOfflineAviso, WhatsappCliente, WhatsappConfig):
        db.query(modelo).delete()
    db.add(
        WhatsappConfig(
            id=1, ativo=True, evolution_url="https://evo.test", api_key_cript="x", instancia="cp",
            minutos_offline=60, lembrete_horas=24, resumo_horario="22:00", ativado_em=AGORA - timedelta(days=30),
        )
    )
    db.commit()
    yield db, enviados
    db.close()


def _cliente(db, telefone="(11) 98888-7777", **opcoes):
    sufixo = uuid.uuid4().hex[:8]
    cliente = Cliente(nome_empresa=f"Cli {sufixo}", email_contato=f"{sufixo}@t.com", api_key=sufixo, telefone=telefone)
    db.add(cliente)
    db.flush()
    valores = {"ativo": True, "ativado_em": AGORA - timedelta(days=30)}
    valores.update(opcoes)
    db.add(WhatsappCliente(cliente_id=cliente.id, **valores))
    db.commit()
    return cliente


def _maquina(db, cliente, ultimo_sinal, horarios=None, pontuais=None):
    maquina = Maquina(id_hardware=f"W{uuid.uuid4().hex[:6]}", nome_local="Grua", cliente_id=cliente.id, ultimo_sinal=ultimo_sinal)
    if horarios:
        salvar_horarios(maquina, horarios)
    if pontuais:
        salvar_pontuais(maquina, pontuais, AGORA)
    db.add(maquina)
    db.commit()
    return maquina


def test_agrupa_maquinas_do_cliente_numa_mensagem(ambiente):
    db, enviados = ambiente
    cliente = _cliente(db)
    a = _maquina(db, cliente, AGORA - timedelta(minutes=61))
    b = _maquina(db, cliente, AGORA - timedelta(minutes=65))
    _maquina(db, cliente, AGORA - timedelta(minutes=30))

    assert whatsapp_alertas.verificar_e_enviar(db, AGORA)["offline"] == 1
    assert len(enviados) == 1
    numero, texto = enviados[0]
    assert numero == "5511988887777"
    assert "(2)" in texto and a.id_hardware in texto and b.id_hardware in texto
    # Proximo ciclo: mesma queda, nao repete.
    assert whatsapp_alertas.verificar_e_enviar(db, AGORA + timedelta(minutes=1))["offline"] == 0


def test_cliente_sem_aviso_offline_nao_recebe(ambiente):
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
    whatsapp_alertas.verificar_e_enviar(db, AGORA)  # 12:00, 30 min depois de abrir
    assert enviados == []
    whatsapp_alertas.verificar_e_enviar(db, AGORA + timedelta(minutes=61))  # 12:31
    assert len(enviados) == 1


def test_fechamento_pontual_nao_avisa(ambiente):
    db, enviados = ambiente
    cliente = _cliente(db)
    _maquina(
        db, cliente, AGORA - timedelta(hours=5),
        pontuais=[{"inicio": "2026-10-26T00:00", "fim": "2026-10-26T23:59", "motivo": "Feriado"}],
    )
    whatsapp_alertas.verificar_e_enviar(db, AGORA)
    assert enviados == []


def test_ja_offline_antes_de_ativar_nao_avisa(ambiente):
    db, enviados = ambiente
    cliente = _cliente(db, ativado_em=AGORA)
    _maquina(db, cliente, AGORA - timedelta(hours=5))
    whatsapp_alertas.verificar_e_enviar(db, AGORA)
    assert enviados == []


def test_voltou_so_para_quem_escolheu(ambiente):
    db, enviados = ambiente
    com = _cliente(db, recebe_voltou=True)
    sem = _cliente(db, telefone="21 97777-6666")
    m1 = _maquina(db, com, AGORA - timedelta(hours=2))
    m2 = _maquina(db, sem, AGORA - timedelta(hours=2))
    whatsapp_alertas.verificar_e_enviar(db, AGORA)
    assert len(enviados) == 2
    m1.ultimo_sinal = m2.ultimo_sinal = AGORA + timedelta(minutes=5)
    db.commit()
    assert whatsapp_alertas.verificar_e_enviar(db, AGORA + timedelta(minutes=5))["voltou"] == 1
    assert enviados[-1][0] == "5511988887777" and "Voltaram" in enviados[-1][1]
    assert db.query(WhatsappOfflineAviso).filter(WhatsappOfflineAviso.resolvido_em.is_(None)).count() == 0


def test_lembrete_a_cada_24h(ambiente):
    db, enviados = ambiente
    cliente = _cliente(db, recebe_lembrete=True)
    _maquina(db, cliente, AGORA - timedelta(hours=2))
    whatsapp_alertas.verificar_e_enviar(db, AGORA)
    assert whatsapp_alertas.verificar_e_enviar(db, AGORA + timedelta(hours=23))["lembrete"] == 0
    assert whatsapp_alertas.verificar_e_enviar(db, AGORA + timedelta(hours=24))["lembrete"] == 1
    assert "Lembrete" in enviados[-1][1]
    assert whatsapp_alertas.verificar_e_enviar(db, AGORA + timedelta(hours=25))["lembrete"] == 0


def test_sem_lembrete_quando_cliente_nao_escolheu(ambiente):
    db, enviados = ambiente
    cliente = _cliente(db)
    _maquina(db, cliente, AGORA - timedelta(hours=2))
    whatsapp_alertas.verificar_e_enviar(db, AGORA)
    assert whatsapp_alertas.verificar_e_enviar(db, AGORA + timedelta(hours=30))["lembrete"] == 0


def test_resumo_diario_uma_vez_no_horario(ambiente, monkeypatch):
    db, enviados = ambiente
    monkeypatch.setattr(
        whatsapp_alertas, "compute_financial_summary_by_machine",
        lambda db, ids, inicio, fim: {i: {"faturamento_total": 150.5, "vendas_count": 30} for i in ids},
    )
    cliente = _cliente(db, ativo=False, resumo_diario=True, resumo_horario="21:30")
    _maquina(db, cliente, AGORA)
    noite = datetime(2026, 10, 27, 0, 15)  # 26/10 21:15 em Brasilia
    assert whatsapp_alertas.verificar_e_enviar(db, noite)["resumo"] == 0
    assert whatsapp_alertas.verificar_e_enviar(db, noite + timedelta(minutes=20))["resumo"] == 1
    assert "R$ 150,50" in enviados[-1][1] and "26/10/2026" in enviados[-1][1]
    assert whatsapp_alertas.verificar_e_enviar(db, noite + timedelta(minutes=40))["resumo"] == 0


def test_outro_processo_com_a_trava_nao_envia(ambiente):
    db, enviados = ambiente
    cliente = _cliente(db)
    _maquina(db, cliente, AGORA - timedelta(hours=2))
    config = db.get(WhatsappConfig, 1)
    config.worker_lock_id = "outro"
    config.worker_lock_ate = datetime.utcnow() + timedelta(minutes=5)
    db.commit()
    assert whatsapp_alertas.verificar_e_enviar(db, AGORA)["pulado"] == "outro_processo"
    assert enviados == []
    # Trava vencida: este processo assume.
    config.worker_lock_ate = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    assert whatsapp_alertas.verificar_e_enviar(db, AGORA)["offline"] == 1


def test_desconectado_nao_envia_nem_marca(ambiente, monkeypatch):
    db, enviados = ambiente
    monkeypatch.setattr(whatsapp_alertas, "estado_conexao", lambda config: "close")
    cliente = _cliente(db)
    _maquina(db, cliente, AGORA - timedelta(hours=2))
    assert whatsapp_alertas.verificar_e_enviar(db, AGORA)["pulado"] == "desconectado"
    assert db.query(WhatsappEnvio).count() == 0
    assert db.query(WhatsappOfflineAviso).count() == 0


def test_usa_telefone_do_perfil_do_usuario(ambiente):
    db, enviados = ambiente
    cliente = _cliente(db, telefone="(11) 90000-0000")
    db.add(Usuario(email=f"u{cliente.id}@t.com", hashed_password="x", cliente_id=cliente.id, telefone="21 97777-6666"))
    db.commit()
    _maquina(db, cliente, AGORA - timedelta(hours=2))
    whatsapp_alertas.verificar_e_enviar(db, AGORA)
    assert [numero for numero, _ in enviados] == ["5521977776666"]


def test_mensagem_e_telefone():
    assert whatsapp_alertas.normalizar_telefone("011 98888-7777") == "5511988887777"
    assert whatsapp_alertas.normalizar_telefone("123") is None
    texto = whatsapp_alertas.montar_mensagem("Oi {cliente} {chave_solta} {maquina}", {"cliente": "A", "maquina": "B"})
    assert texto == "Oi A {chave_solta} B"
    assert whatsapp_alertas.formatar_moeda(1234.5) == "R$ 1.234,50"

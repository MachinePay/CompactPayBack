import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db"
os.environ["START_MQTT_WORKER"] = "false"
os.environ["START_COMMAND_QUEUE_WORKER"] = "false"
os.environ["START_RETENTION_WORKER"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.core.security import get_password_hash
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import (
    EventoTipo,
    HistoricoOperacao,
    Maquina,
    MetodoPagamento,
    Transacao,
    UserRole,
    Usuario,
    VendaPagamento,
)
from app.services.maquinas_relatorio import (
    latest_payment_by_machine,
    latest_pulse_by_machine,
    latest_transacao_in_by_machine,
    noise_counts_by_machine,
)
from app.main import app

Base.metadata.create_all(bind=engine)


def _create_maquina(id_hardware, **kwargs):
    db = SessionLocal()
    try:
        maquina = db.query(Maquina).filter(Maquina.id_hardware == id_hardware).first()
        if not maquina:
            maquina = Maquina(id_hardware=id_hardware, nome_local=id_hardware)
            db.add(maquina)
        for key, value in kwargs.items():
            setattr(maquina, key, value)
        db.commit()
    finally:
        db.close()


def _add_venda(machine_id, **kwargs):
    db = SessionLocal()
    try:
        defaults = dict(
            maquina_id=machine_id,
            origem="digital",
            provider="mercado_pago",
            valor_bruto=kwargs.get("valor_liquido", 0.0),
            valor_liquido=0.0,
            conta_faturamento=True,
            conta_ticket_medio=True,
            created_at=datetime.utcnow(),
        )
        defaults.update(kwargs)
        db.add(VendaPagamento(**defaults))
        db.commit()
    finally:
        db.close()


def _add_transacao(machine_id, tipo=EventoTipo.in_flux, metodo=MetodoPagamento.fisico, valor=1.0, data_hora=None):
    db = SessionLocal()
    try:
        db.add(
            Transacao(
                maquina_id=machine_id,
                tipo=tipo,
                metodo=metodo,
                valor=valor,
                data_hora=data_hora or datetime.utcnow(),
            )
        )
        db.commit()
    finally:
        db.close()


def _add_historico(machine_id, categoria="DISPOSITIVO", descricao="evento", pulse_status=None, created_at=None):
    db = SessionLocal()
    try:
        db.add(
            HistoricoOperacao(
                maquina_id=machine_id,
                categoria=categoria,
                descricao=descricao,
                pulse_status=pulse_status,
                created_at=created_at or datetime.utcnow(),
            )
        )
        db.commit()
    finally:
        db.close()


def test_latest_payment_by_machine_picks_most_recent_per_machine():
    machine_a = "CPM-HEALTH-PAY-A"
    machine_b = "CPM-HEALTH-PAY-B"
    _create_maquina(machine_a)
    _create_maquina(machine_b)

    _add_venda(machine_a, valor_liquido=5.0, created_at=datetime.utcnow() - timedelta(hours=2))
    _add_venda(machine_a, valor_liquido=9.0, created_at=datetime.utcnow() - timedelta(minutes=1))
    _add_venda(machine_b, valor_liquido=20.0, created_at=datetime.utcnow() - timedelta(hours=1))

    db = SessionLocal()
    try:
        result = latest_payment_by_machine(db, [machine_a, machine_b])
    finally:
        db.close()

    assert result[machine_a]["valor"] == 9.0
    assert result[machine_b]["valor"] == 20.0


def test_latest_payment_by_machine_returns_empty_for_machine_with_no_sales():
    machine_id = "CPM-HEALTH-PAY-EMPTY"
    _create_maquina(machine_id)

    db = SessionLocal()
    try:
        result = latest_payment_by_machine(db, [machine_id])
    finally:
        db.close()

    assert machine_id not in result


def test_latest_transacao_in_by_machine_ignores_out_transactions():
    machine_id = "CPM-HEALTH-LEGACY"
    _create_maquina(machine_id)
    _add_transacao(machine_id, tipo=EventoTipo.out_flux, valor=0.0, data_hora=datetime.utcnow())
    _add_transacao(
        machine_id,
        tipo=EventoTipo.in_flux,
        metodo=MetodoPagamento.fisico,
        valor=3.0,
        data_hora=datetime.utcnow() - timedelta(minutes=5),
    )
    _add_transacao(
        machine_id,
        tipo=EventoTipo.in_flux,
        metodo=MetodoPagamento.fisico,
        valor=4.0,
        data_hora=datetime.utcnow() - timedelta(minutes=1),
    )

    db = SessionLocal()
    try:
        result = latest_transacao_in_by_machine(db, [machine_id])
    finally:
        db.close()

    assert result[machine_id]["valor"] == 4.0
    assert result[machine_id]["pulse_status"] == "fisico"


def test_latest_pulse_by_machine_ignores_rows_without_pulse_status():
    machine_id = "CPM-HEALTH-PULSE"
    _create_maquina(machine_id)
    _add_historico(machine_id, descricao="sem pulso", pulse_status=None, created_at=datetime.utcnow())
    _add_historico(
        machine_id,
        descricao="pulso antigo",
        pulse_status="pulso_confirmado",
        created_at=datetime.utcnow() - timedelta(minutes=10),
    )
    _add_historico(
        machine_id,
        descricao="pulso recente",
        pulse_status="falha_timeout",
        created_at=datetime.utcnow() - timedelta(minutes=1),
    )

    db = SessionLocal()
    try:
        result = latest_pulse_by_machine(db, [machine_id])
    finally:
        db.close()

    assert result[machine_id]["status"] == "falha_timeout"


def test_noise_counts_by_machine_counts_matching_patterns_inside_window():
    machine_id = "CPM-HEALTH-NOISE"
    _create_maquina(machine_id)
    since = datetime.utcnow() - timedelta(hours=24)
    for _ in range(3):
        _add_historico(machine_id, descricao="STATUS|COIN_PULSE_CURTO_IGNORADO|width_ms=5", created_at=datetime.utcnow())
    _add_historico(machine_id, descricao="STATUS|ONLINE|fw=1.0.0", created_at=datetime.utcnow())
    _add_historico(
        machine_id,
        descricao="STATUS|COIN_PULSE_CURTO_IGNORADO|width_ms=5",
        created_at=since - timedelta(hours=1),
    )

    db = SessionLocal()
    try:
        result = noise_counts_by_machine(db, [machine_id], since)
    finally:
        db.close()

    assert result[machine_id] == 3


def _create_admin_token(client, email):
    db = SessionLocal()
    try:
        db.add(Usuario(email=email, hashed_password=get_password_hash("123456"), role=UserRole.admin))
        db.commit()
    finally:
        db.close()
    response = client.post("/api/v1/login", data={"username": email, "password": "123456"})
    return response.json()["access_token"]


def test_saude_endpoint_flags_offline_wifi_ruim_and_pulso_ausente():
    machine_offline = "CPM-HEALTH-EP-OFFLINE"
    machine_wifi_ruim = "CPM-HEALTH-EP-WIFI"
    _create_maquina(machine_offline, ultimo_sinal=None)
    _create_maquina(
        machine_wifi_ruim,
        ultimo_sinal=datetime.utcnow(),
        wifi_quality=10,
    )
    _add_venda(machine_wifi_ruim, valor_liquido=15.0, status_pulso="falha_timeout")
    # "ultimo_pulso" no painel de saude vem de HistoricoOperacao.pulse_status (nao
    # de VendaPagamento.status_pulso), entao o fixture precisa gravar os dois.
    _add_historico(machine_wifi_ruim, pulse_status="falha_timeout")

    with TestClient(app) as client:
        token = _create_admin_token(client, "admin-saude@test.local")
        headers = {"Authorization": f"Bearer {token}"}
        response = client.get("/api/v1/maquinas/saude", headers=headers)

    assert response.status_code == 200
    data = response.json()
    by_id = {item["id_hardware"]: item for item in data["maquinas"]}

    assert by_id[machine_offline]["health_status"] == "offline"
    assert by_id[machine_wifi_ruim]["wifi_status"] == "ruim"
    assert by_id[machine_wifi_ruim]["pulse_alert"] is True
    assert by_id[machine_wifi_ruim]["ultimo_pagamento"]["valor"] == 15.0
    assert by_id[machine_wifi_ruim]["health_status"] == "atencao"


def test_alertas_endpoint_generates_pulso_ausente_and_ruido_alerts():
    machine_id = "CPM-HEALTH-EP-ALERT"
    _create_maquina(machine_id, ultimo_sinal=datetime.utcnow(), wifi_quality=90)
    _add_venda(machine_id, valor_liquido=30.0, status_pulso="falha_sem_confirmacao")
    _add_historico(machine_id, pulse_status="falha_sem_confirmacao")
    for _ in range(10):
        _add_historico(machine_id, descricao="STATUS|COIN_PULSE_CURTO_IGNORADO|width_ms=5", created_at=datetime.utcnow())

    with TestClient(app) as client:
        token = _create_admin_token(client, "admin-alertas@test.local")
        headers = {"Authorization": f"Bearer {token}"}
        response = client.get("/api/v1/maquinas/alertas", headers=headers)

    assert response.status_code == 200
    data = response.json()
    alert_types = {
        alert["tipo"] for alert in data["alertas"] if alert["maquina"]["id_hardware"] == machine_id
    }
    assert "pulso_ausente" in alert_types
    assert "ruido_contador" in alert_types

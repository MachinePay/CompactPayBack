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
from app.services.maquinas_relatorio import serialize_machine_summary, serialize_machines_summary_batch
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


def _get_maquina(id_hardware):
    db = SessionLocal()
    try:
        return db.query(Maquina).filter(Maquina.id_hardware == id_hardware).first()
    finally:
        db.close()


def _query_maquinas(db, ids):
    # Busca as maquinas na MESMA sessao que sera usada para serializa-las - igual
    # ao endpoint real - para nao acessar um relacionamento lazy (dono) apos a
    # sessao original ja ter sido fechada.
    return [db.query(Maquina).filter(Maquina.id_hardware == machine_id).first() for machine_id in ids]


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


def _add_transacao(machine_id, tipo, valor=1.0, data_hora=None):
    db = SessionLocal()
    try:
        db.add(
            Transacao(
                maquina_id=machine_id,
                tipo=tipo,
                metodo=MetodoPagamento.fisico,
                valor=valor,
                data_hora=data_hora or datetime.utcnow(),
            )
        )
        db.commit()
    finally:
        db.close()


def _add_teste_historico(machine_id, valor, created_at=None):
    db = SessionLocal()
    try:
        db.add(
            HistoricoOperacao(
                maquina_id=machine_id,
                categoria="TESTE",
                descricao="teste",
                valor=valor,
                created_at=created_at or datetime.utcnow(),
            )
        )
        db.commit()
    finally:
        db.close()


def test_batch_matches_serialize_machine_summary_per_machine():
    machine_a = "CPM-LIST-A"
    machine_b = "CPM-LIST-B"
    _create_maquina(machine_a, ultimo_sinal=datetime.utcnow())
    _create_maquina(machine_b, ultimo_sinal=None)

    _add_venda(machine_a, origem="fisico", provider="fisico", valor_liquido=12.0)
    _add_transacao(machine_a, EventoTipo.out_flux, valor=0.0)
    _add_transacao(machine_a, EventoTipo.out_flux, valor=0.0, data_hora=datetime.utcnow() - timedelta(minutes=5))
    _add_teste_historico(machine_a, 3.0)

    _add_venda(machine_b, valor_liquido=50.0)

    db = SessionLocal()
    try:
        maquinas = _query_maquinas(db, [machine_a, machine_b])
        individuais = [serialize_machine_summary(db, maquina, periodo="mes") for maquina in maquinas]
    finally:
        db.close()

    db = SessionLocal()
    try:
        maquinas = _query_maquinas(db, [machine_a, machine_b])
        em_lote = serialize_machines_summary_batch(db, maquinas, periodo="mes")
    finally:
        db.close()

    # Remove os campos que passam por comparacao de igualdade "==" com datetimes
    # vindos de fontes diferentes (chamadas em instantes ligeiramente distintos)
    # nao muda o resultado de negocio, mas evita falso-negativo por microsegundos.
    for individual, batch in zip(individuais, em_lote):
        assert individual == batch, individual["id_hardware"]


def test_batch_marks_stuck_ota_update_as_failed_like_individual_does():
    machine_id = "CPM-LIST-OTA"
    _create_maquina(
        machine_id,
        ultimo_sinal=datetime.utcnow(),
        firmware_update_status="downloading",
        firmware_update_started_at=datetime.utcnow() - timedelta(minutes=10),
    )
    db = SessionLocal()
    try:
        maquinas = _query_maquinas(db, [machine_id])
        resultado = serialize_machines_summary_batch(db, maquinas, periodo="mes")
    finally:
        db.close()

    assert resultado[0]["firmware_update_status"] == "failed"
    atualizado = _get_maquina(machine_id)
    assert atualizado.firmware_update_status == "failed"


def _create_admin_token(client, email):
    db = SessionLocal()
    try:
        db.add(Usuario(email=email, hashed_password=get_password_hash("123456"), role=UserRole.admin))
        db.commit()
    finally:
        db.close()
    response = client.post("/api/v1/login", data={"username": email, "password": "123456"})
    return response.json()["access_token"]


def test_listar_maquinas_endpoint_returns_batched_summary():
    machine_id = "CPM-LIST-ENDPOINT"
    _create_maquina(machine_id, ultimo_sinal=datetime.utcnow())
    _add_venda(machine_id, valor_liquido=42.0)

    with TestClient(app) as client:
        token = _create_admin_token(client, "admin-list@test.local")
        headers = {"Authorization": f"Bearer {token}"}
        response = client.get("/api/v1/maquinas?periodo=mes", headers=headers)

    assert response.status_code == 200
    data = response.json()
    item = next(m for m in data if m["id_hardware"] == machine_id)
    assert item["faturamento"] == 42.0
    assert item["status_online"] is True

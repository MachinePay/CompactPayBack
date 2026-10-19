import os
import sys
import tempfile
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
from app.models.models import Cliente, Maquina, UserRole, Usuario
from app.services.sumup_poller import _resolve_maquina
from app.main import app

Base.metadata.create_all(bind=engine)


def test_pix_sem_codigo_do_reader_vai_para_a_maquina_do_usuario():
    m14 = Maquina(id_hardware="S-1014", nome_local="1014", sumup_usuario="maquina1014@loja.com")
    m16 = Maquina(id_hardware="S-1016", nome_local="1016", sumup_usuario="maquina1016@loja.com")
    por_usuario = {"maquina1014@loja.com": m14, "maquina1016@loja.com": m16}

    maquina, motivo = _resolve_maquina(None, {}, {}, None, {}, usuario="Maquina1016@Loja.com", maquina_by_usuario=por_usuario)
    assert maquina is m16 and motivo == "verificado_por_usuario"

    # Usuario desconhecido (ex.: dono da conta logado nas duas): continua pendente.
    maquina, motivo = _resolve_maquina(None, {}, {}, None, {}, usuario="dono@loja.com", maquina_by_usuario=por_usuario)
    assert maquina is None and motivo == "sem_device_info_multiplas_maquinas"

    # Codigo do reader (cartao) continua tendo prioridade.
    maquina, motivo = _resolve_maquina("123", {"123": m14}, {}, None, {}, usuario="maquina1016@loja.com", maquina_by_usuario=por_usuario)
    assert maquina is m14 and motivo == "verificado_por_device_code"


def test_mesmo_usuario_sumup_nao_pode_ficar_em_duas_maquinas_do_cliente():
    db = SessionLocal()
    try:
        cliente = Cliente(nome_empresa="Cli SumUp", email_contato="cli@sumup.test", api_key="k-sumup-pix")
        db.add(cliente)
        db.commit()
        db.add(Maquina(id_hardware="S-A", nome_local="A", cliente_id=cliente.id, sumup_usuario="op1@loja.com"))
        db.add(Maquina(id_hardware="S-B", nome_local="B", cliente_id=cliente.id))
        db.add(Usuario(email="admin-sumup@test.local", hashed_password=get_password_hash("123456"), role=UserRole.admin))
        db.commit()
        cliente_id = cliente.id
    finally:
        db.close()

    with TestClient(app) as client:
        token = client.post("/api/v1/login", data={"username": "admin-sumup@test.local", "password": "123456"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        repetido = client.put(
            "/api/v1/maquinas/S-B", json={"nome": "B", "cliente_id": cliente_id, "sumup_usuario": "OP1@loja.com"}, headers=headers
        )
        assert repetido.status_code == 422
        ok = client.put(
            "/api/v1/maquinas/S-B", json={"nome": "B", "cliente_id": cliente_id, "sumup_usuario": "op2@loja.com"}, headers=headers
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["sumup_usuario"] == "op2@loja.com"


def test_maquina_unica_com_usuario_so_credita_pix_do_usuario_dela():
    m14 = Maquina(id_hardware="S-1014", nome_local="1014", sumup_usuario="maquina1014@loja.com")
    por_usuario = {"maquina1014@loja.com": m14}

    maquina, motivo = _resolve_maquina(None, {}, {}, m14, {}, usuario="maquina1014@loja.com", maquina_by_usuario=por_usuario)
    assert maquina is m14 and motivo == "verificado_por_usuario"

    # Outro usuario na mesma conta SumUp: nao credita so porque a maquina e' unica.
    maquina, motivo = _resolve_maquina(None, {}, {}, m14, {}, usuario="dono@loja.com", maquina_by_usuario=por_usuario)
    assert maquina is None and motivo == "usuario_nao_confere"

    # Maquina unica sem usuario cadastrado continua no fallback antigo.
    m16 = Maquina(id_hardware="S-1016", nome_local="1016")
    maquina, motivo = _resolve_maquina(None, {}, {}, m16, {}, usuario="dono@loja.com", maquina_by_usuario={})
    assert maquina is m16 and motivo == "fallback_sem_device_info"

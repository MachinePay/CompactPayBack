"""Regra de acesso (app/core/escopo.py): cliente so ve as proprias maquinas.

Dois clientes, uma maquina cada. O cliente A tenta, por todas as rotas que
mostram dado de maquina, ver a maquina do cliente B - tem que dar 404 ou
simplesmente nao aparecer. Admin ve as duas.
"""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db"
os.environ["START_MQTT_WORKER"] = "false"
os.environ["START_COMMAND_QUEUE_WORKER"] = "false"
os.environ["START_RETENTION_WORKER"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from jose import jwt

from app.core.security import ALGORITHM, SECRET_KEY, get_password_hash
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import (
    Cliente,
    EscutaTerminal,
    Maquina,
    MaquinaWifiCredencial,
    UserRole,
    Usuario,
    VendaPagamento,
)
from app.main import app

Base.metadata.create_all(bind=engine)

FW = "version_2.8.4-enxuto-2026-10-14"


def _setup():
    agora = datetime.utcnow()
    db = SessionLocal()
    try:
        a = Cliente(nome_empresa="Cliente A", email_contato="a@escopo.test", api_key="k-esc-a")
        b = Cliente(nome_empresa="Cliente B", email_contato="b@escopo.test", api_key="k-esc-b")
        db.add_all([a, b])
        db.commit()
        db.add(Maquina(id_hardware="ESC-A", nome_local="Maq A", cliente_id=a.id, ultimo_sinal=agora,
                       firmware_version=FW, latitude=-23.5, longitude=-46.6))
        db.add(Maquina(id_hardware="ESC-B", nome_local="Maq B", cliente_id=b.id, ultimo_sinal=agora,
                       firmware_version=FW, latitude=-22.9, longitude=-43.1))
        db.commit()
        db.add(VendaPagamento(maquina_id="ESC-B", origem="digital", valor_bruto=50.0, valor_liquido=50.0, created_at=agora))
        db.add(MaquinaWifiCredencial(maquina_id="ESC-B", ssid="RedeDoB", senha_cript="", credencial_hash="h-b",
                                     primeiro_visto_em=agora, ultimo_visto_em=agora))
        db.add(EscutaTerminal(terminal_id="TERM-B", maquina_id="ESC-B", ativo=True, created_at=agora, updated_at=agora))
        db.add(Usuario(email="admin@escopo.test", hashed_password=get_password_hash("123456"), role=UserRole.admin))
        db.add(Usuario(email="dono-a@escopo.test", hashed_password=get_password_hash("123456"),
                       role=UserRole.cliente, cliente_id=a.id))
        db.commit()
        return a.id, b.id
    finally:
        db.close()


def _headers(client, email):
    token = client.post("/api/v1/login", data={"username": email, "password": "123456"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _ids(lista, chave="maquina_id"):
    return {item[chave] for item in lista}


def test_cliente_nunca_ve_maquina_de_outro_cliente():
    _, cliente_b = _setup()
    with TestClient(app) as client:
        a = _headers(client, "dono-a@escopo.test")
        admin = _headers(client, "admin@escopo.test")

        # Listas: so a propria, mesmo pedindo o cliente B.
        assert _ids(client.get("/api/v1/maquinas", headers=a).json(), "id_hardware") == {"ESC-A"}
        assert _ids(client.get(f"/api/v1/mapa/maquinas?cliente_id={cliente_b}", headers=a).json()["maquinas"]) == {"ESC-A"}
        ranking = client.get(f"/api/v1/ranking-maquinas?periodo=dia&cliente_id={cliente_b}", headers=a).json()
        assert _ids(ranking["maquinas"]) == {"ESC-A"}
        feed = client.get(f"/api/v1/ao-vivo?cliente_id={cliente_b}", headers=a).json()
        assert _ids(feed["eventos"]) <= {"ESC-A"}
        assert feed["hoje"]["faturamento"] == 0
        pico = client.get("/api/v1/horarios-pico?periodo=semana&machine_id=ESC-B", headers=a).json()
        assert all(celula["vendas"] == 0 for linha in pico["grade"] for celula in linha)
        assert client.get("/api/v1/wifi/historico?todas=true", headers=a).json()["redes"] == []
        assert client.get(f"/api/v1/wifi/historico?cliente_id={cliente_b}", headers=a).json()["redes"] == []
        assert client.get("/api/v1/pagamentos/escuta", headers=a).json()["ativos"] == []

        # Maquina do outro pelo ID: 404 (nem revela que existe).
        for metodo, url in (
            ("get", "/api/v1/maquinas/ESC-B/historico"),
            ("get", "/api/v1/maquinas/ESC-B/wifi/redes"),
            ("post", "/api/v1/maquinas/ESC-B/wifi/procurar"),
            ("post", "/api/v1/maquinas/ESC-B/wifi/solicitar"),
            ("post", "/api/v1/maquinas/ESC-B/verificar-online"),
        ):
            resposta = getattr(client, metodo)(url, headers=a)
            assert resposta.status_code == 404, (url, resposta.status_code)
        troca = client.post("/api/v1/maquinas/ESC-B/wifi", json={"ssid": "X", "senha": "12345678"}, headers=a)
        assert troca.status_code == 404
        parar = client.post("/api/v1/pagamentos/escuta/parar", json={"terminal_id": "TERM-B"}, headers=a)
        assert parar.status_code == 404

        # Admin ve as duas.
        assert _ids(client.get("/api/v1/mapa/maquinas", headers=admin).json()["maquinas"]) == {"ESC-A", "ESC-B"}
        assert client.get("/api/v1/wifi/historico?todas=true", headers=admin).json()["redes"][0]["ssid"] == "RedeDoB"


def test_rotas_de_escuta_exigem_login():
    with TestClient(app) as client:
        assert client.get("/api/v1/pagamentos/escuta").status_code == 401
        assert client.post("/api/v1/pagamentos/escuta/parar", json={"terminal_id": "x"}).status_code == 401


def test_login_de_cliente_sem_cliente_vinculado_e_recusado():
    # Sem essa trava, "Maquina.cliente_id == None" viraria IS NULL e mostraria maquinas sem dono.
    token = jwt.encode(
        {"sub": "sem-cliente@escopo.test", "role": "cliente", "cliente_id": None}, SECRET_KEY, algorithm=ALGORITHM
    )
    with TestClient(app) as client:
        resposta = client.get("/api/v1/maquinas", headers={"Authorization": f"Bearer {token}"})
    assert resposta.status_code == 403

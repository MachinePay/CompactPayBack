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
from app.models.models import EventoTipo, HistoricoOperacao, Maquina, Transacao, UserRole, Usuario, VendaPagamento
from app.main import app

Base.metadata.create_all(bind=engine)


def _setup():
    agora = datetime.utcnow()
    db = SessionLocal()
    try:
        for mid in ("RK-A", "RK-B", "RK-C"):
            db.add(Maquina(id_hardware=mid, nome_local=f"Maquina {mid}", pulse_value="1", ultimo_sinal=agora))
        db.commit()
        # A e B: R$ 10 por pelucia; C: R$ 1 por pelucia (dando demais).
        for mid, valor, pelucias in (("RK-A", 100.0, 10), ("RK-B", 60.0, 6), ("RK-C", 40.0, 40)):
            db.add(VendaPagamento(maquina_id=mid, origem="digital", valor_bruto=valor, valor_liquido=valor, created_at=agora - timedelta(minutes=5)))
            for _ in range(pelucias):
                db.add(Transacao(maquina_id=mid, tipo=EventoTipo.out_flux, valor=1.0, data_hora=agora - timedelta(minutes=4)))
        db.add(HistoricoOperacao(maquina_id="RK-B", categoria="DISPOSITIVO", descricao="Maquina caiu (MQTT last will)", created_at=agora - timedelta(minutes=3)))
        db.add(Usuario(email="admin-rk@test.local", hashed_password=get_password_hash("123456"), role=UserRole.admin))
        db.commit()
    finally:
        db.close()


def test_ao_vivo_e_ranking():
    _setup()
    with TestClient(app) as client:
        token = client.post("/api/v1/login", data={"username": "admin-rk@test.local", "password": "123456"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        feed = client.get("/api/v1/ao-vivo?limit=100", headers=headers).json()
        ranking = client.get("/api/v1/ranking-maquinas?periodo=dia", headers=headers).json()

    tipos = {e["tipo"] for e in feed["eventos"]}
    assert {"pagamento", "pelucia", "queda"} <= tipos
    pagamento_a = next(e for e in feed["eventos"] if e["tipo"] == "pagamento" and e["maquina_id"] == "RK-A")
    assert pagamento_a["jogadas"] == 100
    assert feed["eventos"][0]["tipo"] == "queda"  # mais recente primeiro
    assert feed["hoje"]["destaque"]["maquina_id"] == "RK-A"

    por_id = {m["maquina_id"]: m for m in ranking["maquinas"]}
    assert por_id["RK-B"]["quedas"] == 1
    assert por_id["RK-A"]["reais_por_pelucia"] == 10.0
    assert por_id["RK-C"]["payout_situacao"] == "dando_demais"
    assert por_id["RK-A"]["payout_situacao"] == "normal"


def test_horarios_pico_agrupa_em_horario_de_brasilia():
    db = SessionLocal()
    try:
        db.add(Maquina(id_hardware="HP-1", nome_local="HP-1"))
        db.commit()
        # 2026-10-13 23:30 UTC = terca 20:30 em Brasilia.
        db.add(VendaPagamento(maquina_id="HP-1", origem="digital", valor_bruto=7.0, valor_liquido=7.0, created_at=datetime(2026, 10, 13, 23, 30)))
        db.add(Usuario(email="admin-hp@test.local", hashed_password=get_password_hash("123456"), role=UserRole.admin))
        db.commit()
    finally:
        db.close()
    from unittest.mock import patch
    import app.api.v1.endpoints.ao_vivo as modulo

    class Relogio(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 14, 12, 0, tzinfo=tz)

    with patch.object(modulo, "datetime", Relogio), TestClient(app) as client:
        token = client.post("/api/v1/login", data={"username": "admin-hp@test.local", "password": "123456"}).json()["access_token"]
        data = client.get("/api/v1/horarios-pico?periodo=semana&machine_id=HP-1", headers={"Authorization": f"Bearer {token}"}).json()

    assert data["grade"][1][20]["faturamento"] == 7.0  # terca, 20h
    assert data["grade"][1][20]["vendas"] == 1
    assert sum(data["dias_por_dia_da_semana"]) == 7

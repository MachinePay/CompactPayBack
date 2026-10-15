import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db"
os.environ["START_MQTT_WORKER"] = "false"
os.environ["START_COMMAND_QUEUE_WORKER"] = "false"
os.environ["START_RETENTION_WORKER"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import Maquina
from app.services import geolocalizacao_wifi as geo
import app.main  # noqa: F401  (registra todos os modelos)

Base.metadata.create_all(bind=engine)

APS = "aabbccddee01,-50,6;aabbccddee02,-62,11;aabbccddee03,-70,1"


def _maquina(machine_id, **kwargs):
    db = SessionLocal()
    try:
        db.add(Maquina(id_hardware=machine_id, nome_local=machine_id, **kwargs))
        db.commit()
    finally:
        db.close()


def _ler(machine_id):
    db = SessionLocal()
    try:
        return db.query(Maquina).filter(Maquina.id_hardware == machine_id).first()
    finally:
        db.close()


def test_interpreta_roteadores_no_formato_do_google():
    assert geo.interpretar_roteadores("aabbccddee01,-50,6;lixo;zz,1,2") == [
        {"macAddress": "aa:bb:cc:dd:ee:01", "signalStrength": -50, "channel": 6}
    ]


def test_sem_chave_nao_consulta_e_marca_status(monkeypatch):
    monkeypatch.delenv("GOOGLE_GEOLOCATION_API_KEY", raising=False)
    _maquina("GEO-SEM-CHAVE")
    with patch.object(geo, "consultar_google") as consulta:
        assert geo.localizar_por_wifi("GEO-SEM-CHAVE", APS) == "sem_chave"
    consulta.assert_not_called()


def test_salva_localizacao_e_respeita_limite_de_10_min(monkeypatch):
    monkeypatch.setenv("GOOGLE_GEOLOCATION_API_KEY", "teste")
    _maquina("GEO-OK")
    agora = datetime.utcnow()
    with patch.object(geo, "consultar_google", return_value=(-23.55, -46.63, 40.0)) as consulta:
        assert geo.localizar_por_wifi("GEO-OK", APS, agora) == "ok"
        assert geo.localizar_por_wifi("GEO-OK", APS, agora + timedelta(minutes=3)) == "limite"
    assert consulta.call_count == 1
    maquina = _ler("GEO-OK")
    assert (maquina.latitude, maquina.longitude) == (-23.55, -46.63)
    assert maquina.localizacao_origem == "wifi"
    assert maquina.localizacao_precisao_m == 40.0


def test_nao_troca_gps_do_celular_pelo_wifi_no_mesmo_lugar_mas_troca_se_mudou(monkeypatch):
    monkeypatch.setenv("GOOGLE_GEOLOCATION_API_KEY", "teste")
    _maquina("GEO-GPS", latitude=-23.5500, longitude=-46.6300, localizacao_origem="celular", localizacao_precisao_m=8.0)
    agora = datetime.utcnow()
    # ~40 m dali, menos preciso: mantem o GPS.
    with patch.object(geo, "consultar_google", return_value=(-23.5503, -46.6302, 60.0)):
        assert geo.localizar_por_wifi("GEO-GPS", APS, agora) == "mantida"
    assert _ler("GEO-GPS").localizacao_origem == "celular"
    # Outra cidade: a maquina mudou de lugar.
    with patch.object(geo, "consultar_google", return_value=(-22.90, -43.17, 50.0)):
        assert geo.localizar_por_wifi("GEO-GPS", APS, agora + timedelta(minutes=11)) == "ok"
    assert _ler("GEO-GPS").localizacao_origem == "wifi"


def test_poucos_roteadores_nao_consulta(monkeypatch):
    monkeypatch.setenv("GOOGLE_GEOLOCATION_API_KEY", "teste")
    _maquina("GEO-POUCOS")
    with patch.object(geo, "consultar_google") as consulta:
        assert geo.localizar_por_wifi("GEO-POUCOS", "aabbccddee01,-50,6") == "sem_redes"
    consulta.assert_not_called()

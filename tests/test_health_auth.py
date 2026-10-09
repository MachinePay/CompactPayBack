import os
import sys
import tempfile
from pathlib import Path

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db"
os.environ["START_MQTT_WORKER"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["APP_VERSION"] = "test"
os.environ["APP_REVISION"] = "pytest"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.core.security import get_password_hash
from app.db.session import SessionLocal
from app.main import app
from app.models.models import UserRole, Usuario


def create_user(email="admin@test.local", password="123456", role=UserRole.admin):
    db = SessionLocal()
    try:
        user = Usuario(
            email=email,
            hashed_password=get_password_hash(password),
            role=role,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    finally:
        db.close()


def test_health_check_reports_database_ok():
    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.headers["x-request-id"]
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "compactpay-backend"
    assert payload["database"] == "ok"
    assert payload["version"] == "test"
    assert payload["revision"] == "pytest"


def test_version_endpoint_reports_build_metadata():
    with TestClient(app) as client:
        response = client.get("/api/v1/version")

    assert response.status_code == 200
    assert response.json()["version"] == "test"
    assert response.json()["revision"] == "pytest"


def test_login_with_valid_credentials_returns_bearer_token():
    with TestClient(app) as client:
        create_user()
        response = client.post(
            "/api/v1/login",
            data={"username": "admin@test.local", "password": "123456"},
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["token_type"] == "bearer"
    assert payload["access_token"]


def test_login_with_invalid_credentials_returns_401():
    with TestClient(app) as client:
        create_user(email="invalid@test.local")
        response = client.post(
            "/api/v1/login",
            data={"username": "invalid@test.local", "password": "senha-errada"},
        )

    assert response.status_code == 401


def test_health_reports_mqtt_worker_status():
    from app.services import mqtt_worker

    mqtt_worker.MQTT_WORKER_STATE["connected"] = False
    with TestClient(app) as client:
        data = client.get("/api/v1/health").json()
    # Nos testes o worker vem desligado (START_MQTT_WORKER=false).
    assert data["mqtt"]["status"] in {"disabled", "disconnected"}


def test_mqtt_worker_retries_when_first_connect_fails(monkeypatch):
    """Antes, uma falha no connect() inicial matava a thread do worker."""
    from app.services import mqtt_worker

    class Parar(BaseException):
        pass

    tentativas = {"connect": 0}

    class FakeClient:
        def connect(self, *args, **kwargs):
            tentativas["connect"] += 1
            if tentativas["connect"] < 3:
                raise OSError("AWS IoT nao respondeu")

        def loop_forever(self):
            raise Parar()

    esperas = []
    monkeypatch.setattr(mqtt_worker, "_build_mqtt_client", lambda: FakeClient())
    monkeypatch.setattr(mqtt_worker.time, "sleep", lambda s: esperas.append(s))

    try:
        mqtt_worker.start_mqtt_worker()
    except Parar:
        pass

    assert tentativas["connect"] == 3
    assert esperas == [2, 4]
    assert "AWS IoT nao respondeu" in mqtt_worker.MQTT_WORKER_STATE["last_error"]

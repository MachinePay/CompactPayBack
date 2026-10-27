import gzip
import os
import subprocess
import sys
import tempfile
import textwrap
import uuid
from pathlib import Path

import pytest

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db"
os.environ["START_MQTT_WORKER"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ.pop("RENDER", None)
os.environ.pop("APP_ENV", None)
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient

from app.core import login_limite
from app.core.security import get_password_hash
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.main import app as fastapi_app
from app.models.models import FirmwareVersion, Usuario
import app.models.produto  # noqa: F401

Base.metadata.create_all(bind=engine)
client = TestClient(fastapi_app)


@pytest.fixture(autouse=True)
def limpar_limite():
    login_limite.zerar()
    yield
    login_limite.zerar()


def _usuario(senha="certa123"):
    db = SessionLocal()
    email = f"{uuid.uuid4().hex[:8]}@teste.com"
    db.add(Usuario(email=email, hashed_password=get_password_hash(senha), role="admin"))
    db.commit()
    db.close()
    return email


def _login(email, senha, ip="10.0.0.1"):
    return client.post(
        "/api/v1/login", data={"username": email, "password": senha}, headers={"X-Forwarded-For": ip}
    )


def test_bloqueia_email_depois_de_5_senhas_erradas():
    email = _usuario()
    for _ in range(5):
        assert _login(email, "errada").status_code == 401
    resposta = _login(email, "certa123")  # nem a senha certa entra durante o bloqueio
    assert resposta.status_code == 429
    assert "Muitas tentativas" in resposta.json()["detail"]
    assert int(resposta.headers["Retry-After"]) > 0
    # Outro e-mail, mesmo IP, segue entrando.
    assert _login(_usuario(), "certa123").status_code == 200


def test_login_certo_zera_o_contador():
    email = _usuario()
    for _ in range(4):
        _login(email, "errada")
    assert _login(email, "certa123").status_code == 200
    for _ in range(4):
        assert _login(email, "errada").status_code == 401
    assert _login(email, "certa123").status_code == 200


def test_bloqueia_ip_que_testa_varios_emails():
    for i in range(20):
        _login(f"naoexiste{i}@x.com", "errada", ip="10.9.9.9")
    assert _login(_usuario(), "certa123", ip="10.9.9.9").status_code == 429
    assert _login(_usuario(), "certa123", ip="10.1.1.1").status_code == 200


def test_gzip_nas_respostas_grandes_e_nunca_no_firmware():
    # Fora de producao o /openapi.json existe e e' grande: vem compactado.
    resposta = client.get("/openapi.json", headers={"Accept-Encoding": "gzip"})
    assert resposta.status_code == 200
    assert resposta.headers.get("content-encoding") == "gzip"

    db = SessionLocal()
    conteudo = bytes(range(256)) * 40  # ~10 KB de "firmware"
    db.add(FirmwareVersion(nome="t", url_bin="x", arquivo_nome="teste-gzip.bin", arquivo_bin=conteudo))
    db.commit()
    db.close()
    bruto = client.get(
        "/api/v1/firmware-files/teste-gzip.bin", headers={"Accept-Encoding": "gzip"}
    )
    assert bruto.status_code == 200
    assert "content-encoding" not in bruto.headers
    assert bruto.content == conteudo


def _rodar_em_producao(codigo: str) -> str:
    env = {
        **os.environ,
        "RENDER": "true",
        "DATABASE_URL": f"sqlite:///{tempfile.mkdtemp()}/prod-sim.db",
        "PYTHONPATH": str(BACKEND),
    }
    env.pop("APP_ENV", None)
    env.pop("EXPOSE_API_DOCS", None)
    resultado = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(codigo)], env=env, capture_output=True, text=True, cwd=BACKEND
    )
    assert resultado.returncode == 0, resultado.stderr
    return resultado.stdout.strip()


def test_em_producao_esconde_docs_e_tira_localhost_do_cors():
    saida = _rodar_em_producao(
        """
        from fastapi.testclient import TestClient
        from app.main import app, ALLOWED_ORIGINS
        c = TestClient(app)
        docs = [c.get(p).status_code for p in ("/docs", "/redoc", "/openapi.json")]
        preflight = lambda origem: c.options(
            "/api/v1/login",
            headers={"Origin": origem, "Access-Control-Request-Method": "POST"},
        ).headers.get("access-control-allow-origin")
        print(docs, any("localhost" in o for o in ALLOWED_ORIGINS),
              preflight("http://localhost:5173"), preflight("https://compactpay.com.br"))
        """
    )
    assert saida == "[404, 404, 404] False None https://compactpay.com.br"


def test_ip_inventado_no_cabecalho_nao_burla_o_limite():
    # Atacante manda um X-Forwarded-For diferente a cada tentativa; o proxy
    # acrescenta o IP real (10.7.7.7) no fim.
    for i in range(20):
        _login(f"naoexiste{i}@x.com", "errada", ip=f"1.2.3.{i}, 10.7.7.7")
    assert _login(_usuario(), "certa123", ip="9.9.9.9, 10.7.7.7").status_code == 429


def test_bloqueio_dura_5_minutos():
    email = _usuario()
    for _ in range(5):
        _login(email, "errada")
    resposta = _login(email, "certa123")
    assert resposta.status_code == 429
    assert int(resposta.headers["Retry-After"]) <= 5 * 60
    assert "5 minuto" in resposta.json()["detail"]

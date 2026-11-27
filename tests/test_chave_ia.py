"""Chave da IA Central (X-API-Key): le tudo, so escreve no que a IA usa."""
import os

from fastapi.testclient import TestClient

from app.main import app

CHAVE = "k" * 40


def _cliente(monkeypatch):
    monkeypatch.setenv("IA_CENTRAL_API_KEY", CHAVE)
    return TestClient(app)


def test_chave_le_as_rotas(monkeypatch):
    with _cliente(monkeypatch) as client:
        r = client.get("/api/v1/maquinas", headers={"X-API-Key": CHAVE})
    assert r.status_code == 200


def test_chave_errada_recusada(monkeypatch):
    with _cliente(monkeypatch) as client:
        r = client.get("/api/v1/maquinas", headers={"X-API-Key": "errada"})
    assert r.status_code == 401


def test_sem_chave_configurada_recusa(monkeypatch):
    monkeypatch.setenv("IA_CENTRAL_API_KEY", "")
    with TestClient(app) as client:
        r = client.get("/api/v1/maquinas", headers={"X-API-Key": ""})
    assert r.status_code == 401


def test_chave_nao_escreve_fora_da_lista(monkeypatch):
    with _cliente(monkeypatch) as client:
        r = client.post("/api/v1/usuarios", headers={"X-API-Key": CHAVE}, json={"email": "x@x.com", "password": "123456"})
    assert r.status_code == 403


def test_credito_remoto_passa_pela_chave(monkeypatch):
    with _cliente(monkeypatch) as client:
        r = client.post("/api/v1/maquinas/9999/credito-teste", headers={"X-API-Key": CHAVE}, json={"valor": 1})
    # passou da autenticacao (a maquina nao existe no banco de teste)
    assert r.status_code not in (401, 403)


def test_sem_token_continua_401():
    with TestClient(app) as client:
        r = client.get("/api/v1/maquinas")
    assert r.status_code == 401

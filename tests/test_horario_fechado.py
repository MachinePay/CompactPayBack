from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.horario_fechado import maquina_fechada, ponto_fechado_agora, salvar_horarios
from app.services.maquinas_relatorio import build_machine_alerts, status_operacional


def _maquina(horarios):
    maquina = SimpleNamespace(horario_fechado_json=None)
    salvar_horarios(maquina, horarios)
    return maquina


def _utc(ano, mes, dia, hora, minuto=0):
    # Horario de Brasilia (-03:00) -> UTC naive, igual ao banco.
    return datetime(ano, mes, dia, hora, minuto) + timedelta(hours=3)


# 2026-10-26 e' segunda-feira (dia 0).
SEGUNDA = (2026, 10, 26)
TERCA = (2026, 10, 27)


def test_sem_horario_nunca_fecha():
    maquina = _maquina([])
    assert maquina.horario_fechado_json is None
    assert not ponto_fechado_agora(maquina, _utc(*SEGUNDA, 3))


def test_intervalo_no_mesmo_dia():
    maquina = _maquina([{"dia": 0, "inicio": "12:00", "fim": "14:00"}])
    assert ponto_fechado_agora(maquina, _utc(*SEGUNDA, 13))
    assert not ponto_fechado_agora(maquina, _utc(*SEGUNDA, 14))
    assert not ponto_fechado_agora(maquina, _utc(*TERCA, 13))


def test_intervalo_atravessa_meia_noite():
    maquina = _maquina([{"dia": 0, "inicio": "22:00", "fim": "08:00"}])
    assert ponto_fechado_agora(maquina, _utc(*SEGUNDA, 23))
    assert ponto_fechado_agora(maquina, _utc(*TERCA, 7, 59))
    assert not ponto_fechado_agora(maquina, _utc(*TERCA, 8))
    assert not ponto_fechado_agora(maquina, _utc(*SEGUNDA, 7))


def test_dia_todo():
    maquina = _maquina([{"dia": 6, "inicio": "00:00", "fim": "00:00"}])
    assert ponto_fechado_agora(maquina, _utc(2026, 11, 1, 0))
    assert ponto_fechado_agora(maquina, _utc(2026, 11, 1, 23, 59))
    assert not ponto_fechado_agora(maquina, _utc(2026, 11, 2, 0))


def test_horario_invalido():
    with pytest.raises(ValueError):
        _maquina([{"dia": 0, "inicio": "25:00", "fim": "08:00"}])
    with pytest.raises(ValueError):
        _maquina([{"dia": 7, "inicio": "22:00", "fim": "08:00"}])


def test_fechado_so_quando_sem_sinal():
    maquina = _maquina([{"dia": 0, "inicio": "00:00", "fim": "00:00"}])
    agora = _utc(*SEGUNDA, 10)
    assert maquina_fechada(maquina, False, agora)
    assert not maquina_fechada(maquina, True, agora)
    assert status_operacional(False, None, True) == "fechado"
    assert status_operacional(False, None, False) == "offline"


def test_ponto_fechado_nao_gera_alerta_offline():
    agora = datetime(2026, 10, 26, 12)
    machine = {
        "id_hardware": "1000",
        "nome": "M",
        "cliente_nome": None,
        "localizacao": None,
        "status_online": False,
        "ultimo_sinal": agora - timedelta(hours=2),
        "wifi_status": "bom",
        "firmware_alert": False,
        "pulse_alert": False,
        "ultimo_pagamento": None,
        "ultimo_pulso": None,
    }
    tipos = {a["tipo"] for a in build_machine_alerts({**machine, "ponto_fechado": True}, agora, 0)}
    assert "offline" not in tipos
    tipos = {a["tipo"] for a in build_machine_alerts({**machine, "ponto_fechado": False}, agora, 0)}
    assert "offline" in tipos

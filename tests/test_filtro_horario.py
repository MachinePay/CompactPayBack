import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.maquinas_relatorio import resolve_date_window


def test_so_data_pega_o_dia_inteiro_de_brasilia():
    inicio, fim = resolve_date_window(None, "2026-10-19", "2026-10-19")
    assert inicio == datetime(2026, 10, 19, 3, 0)
    assert fim == datetime(2026, 10, 20, 2, 59, 59, 999999)


def test_data_com_horario_filtra_so_a_janela_e_inclui_o_minuto_final():
    inicio, fim = resolve_date_window(None, "2026-10-19T14:00", "2026-10-19T18:30")
    assert inicio == datetime(2026, 10, 19, 17, 0)
    assert fim == datetime(2026, 10, 19, 21, 30, 59, 999999)


def test_horario_so_no_inicio():
    inicio, fim = resolve_date_window(None, "2026-10-18T22:00", "2026-10-19")
    assert inicio == datetime(2026, 10, 19, 1, 0)
    assert fim == datetime(2026, 10, 20, 2, 59, 59, 999999)

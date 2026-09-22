from datetime import datetime

from app.services.pagamentos_helpers import (
    extract_terminal_id,
    should_allow_refund,
    should_auto_refund_on_pulse_failure,
)


def test_extract_terminal_id_rejects_mercado_pago_placeholder():
    # Pix via QR code (sem maquininha fisica) vem do Mercado Pago com um
    # placeholder tipo "N/A" nesse campo em vez de vir vazio - nao pode ser
    # aceito como se fosse um terminal_id de verdade.
    assert extract_terminal_id({"point_of_interaction": {"transaction_data": {"terminal_id": "N/A"}}}) is None
    assert extract_terminal_id({"terminal_id": "n/a"}) is None
    assert extract_terminal_id({"terminal_id": "null"}) is None
    assert (
        extract_terminal_id({"point_of_interaction": {"transaction_data": {"terminal_id": "Q92-123456"}}})
        == "Q92-123456"
    )


def test_non_released_pulse_requires_auto_refund():
    assert should_auto_refund_on_pulse_failure("falha_publicacao") is True
    assert should_auto_refund_on_pulse_failure("falha_cmd_ignorado") is True
    assert should_auto_refund_on_pulse_failure("falha_bloqueado") is True
    assert should_auto_refund_on_pulse_failure("saldo_pendente") is True
    # Placa nunca respondeu nada (nem CMD_RECEBIDO) - da pra ter certeza que o
    # pulso fisico nunca aconteceu, entao entra no estorno automatico.
    assert should_auto_refund_on_pulse_failure("falha_dispositivo_offline") is True
    assert should_auto_refund_on_pulse_failure("pulso_confirmado") is False


def test_ambiguous_pulse_status_does_not_auto_refund():
    # Comando pode ter sido enviado e executado (ex.: maquina sem contador
    # para confirmar o pulso) - fica so para extorno manual do operador.
    assert should_auto_refund_on_pulse_failure("falha_timeout") is False
    assert should_auto_refund_on_pulse_failure("falha_sem_confirmacao") is False
    assert should_auto_refund_on_pulse_failure("pulso_sem_retorno") is False
    assert should_auto_refund_on_pulse_failure("falha") is False


def test_refund_button_stays_available_after_confirmed_pulse():
    assert should_allow_refund("pulso_confirmado", None, "pay_123", "mercado_pago") is True
    assert should_allow_refund("falha_timeout", None, "pay_123", "mercado_pago") is True
    assert should_allow_refund("pulso_confirmado", datetime.utcnow(), "pay_123", "mercado_pago") is False
    assert should_allow_refund("pulso_confirmado", None, None, "mercado_pago") is False

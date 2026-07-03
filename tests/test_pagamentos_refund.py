from datetime import datetime

from app.services.pagamentos_helpers import should_allow_refund, should_auto_refund_on_pulse_failure


def test_non_released_pulse_requires_auto_refund():
    assert should_auto_refund_on_pulse_failure("falha_timeout") is True
    assert should_auto_refund_on_pulse_failure("falha_publicacao") is True
    assert should_auto_refund_on_pulse_failure("saldo_pendente") is True
    assert should_auto_refund_on_pulse_failure("pulso_confirmado") is False


def test_refund_button_stays_available_after_confirmed_pulse():
    assert should_allow_refund("pulso_confirmado", None, "pay_123", "mercado_pago") is True
    assert should_allow_refund("falha_timeout", None, "pay_123", "mercado_pago") is True
    assert should_allow_refund("pulso_confirmado", datetime.utcnow(), "pay_123", "mercado_pago") is False
    assert should_allow_refund("pulso_confirmado", None, None, "mercado_pago") is False

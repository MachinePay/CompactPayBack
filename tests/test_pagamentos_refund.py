from datetime import datetime
from unittest.mock import patch

from app.services.pagamentos_helpers import (
    extract_terminal_id,
    payment_metadata,
    should_allow_refund,
    should_auto_refund_on_pulse_failure,
)
import app.services.pagamentos_helpers as pagamentos_helpers


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


def test_payment_metadata_resolves_bank_name_from_issuer_id_when_issuer_object_is_missing():
    # Pagamento na maquininha fisica nao traz o objeto "issuer" (nome do banco)
    # - so o issuer_id solto (confirmado com dado real de producao). Precisa
    # de uma segunda consulta na API pra resolver o nome do banco a partir
    # desse id, filtrando pela bandeira certa.
    pagamentos_helpers._CARD_ISSUER_NAME_CACHE.clear()
    payment_data = {
        "id": 180468773516,
        "payment_type_id": "debit_card",
        "payment_method_id": "debvisa",
        "issuer_id": "25",
        "issuer": None,
        "card": {"last_four_digits": "7575"},
    }
    with patch("app.services.pagamentos_helpers.mp_request") as mp_request_mock:
        mp_request_mock.return_value = [
            {"id": 24, "name": "Banco Outro"},
            {"id": 25, "name": "Nubank"},
        ]
        metadata = payment_metadata(payment_data, token="token-teste")

    assert metadata["bank_name"] == "Nubank"
    assert metadata["card_last_four"] == "7575"
    mp_request_mock.assert_called_once()
    called_url = mp_request_mock.call_args.args[1]
    assert "card_issuers" in called_url
    assert "payment_method_id=debvisa" in called_url

    # Segunda chamada com o mesmo issuer_id/bandeira usa o cache, nao bate na API de novo.
    with patch("app.services.pagamentos_helpers.mp_request") as mp_request_mock_2:
        metadata_2 = payment_metadata(payment_data, token="token-teste")
    assert metadata_2["bank_name"] == "Nubank"
    mp_request_mock_2.assert_not_called()


def test_payment_metadata_bank_name_none_when_issuer_lookup_fails():
    pagamentos_helpers._CARD_ISSUER_NAME_CACHE.clear()
    payment_data = {
        "id": 1,
        "payment_type_id": "credit_card",
        "payment_method_id": "master",
        "issuer_id": "99",
        "issuer": None,
        "card": {},
    }
    with patch("app.services.pagamentos_helpers.mp_request") as mp_request_mock:
        mp_request_mock.side_effect = Exception("timeout")
        metadata = payment_metadata(payment_data, token="token-teste")

    assert metadata["bank_name"] is None

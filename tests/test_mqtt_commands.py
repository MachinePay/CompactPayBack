import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db"
os.environ["START_MQTT_WORKER"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["MQTT_COMMAND_QOS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.base import Base
from app.db.session import engine
from app.services.mqtt_commands import publish_machine_credit, publish_machine_credit_pulses
from app.services.mqtt_worker import _status_to_pulse_status
from app.services.pulse_tracking import FINAL_PULSE_STATUSES

Base.metadata.create_all(bind=engine)


def test_publish_machine_credit_uses_expected_topic_payload_and_qos():
    with patch("app.services.mqtt_commands.publish.single") as publish_single:
        payload = publish_machine_credit("CPM-TESTE-1", action="paid", command_id="cmd-1")

    assert payload == "CPM-TESTE-1@paid|cmd=cmd-1|"
    publish_single.assert_called_once()
    _, kwargs = publish_single.call_args
    assert publish_single.call_args.args[0] == "/TEF/CPM-TESTE-1/cmd"
    assert kwargs["payload"] == "CPM-TESTE-1@paid|cmd=cmd-1|"
    assert kwargs["qos"] == 1


def test_publish_machine_credit_pulses_sends_single_payload_with_quantity_and_amount():
    # Maquina diferente da do teste anterior: track_and_publish_command tem
    # uma trava anti-cobranca-dupla que poe um comando novo na fila (sem
    # publicar na hora) se a MESMA maquina ja tiver um comando de credito em
    # andamento - o comando "cmd-1" do teste acima nunca e' finalizado, entao
    # reusar a mesma maquina aqui faria esse comando cair na fila e o mock do
    # publish.single nunca seria chamado.
    with patch("app.services.mqtt_commands.publish.single") as publish_single:
        payload = publish_machine_credit_pulses("CPM-TESTE-2", pulses=5, command_id="cmd-2", amount=5.0)

    assert payload == "CPM-TESTE-2@paid|cmd=cmd-2|amount=5.00|"
    publish_single.assert_called_once()
    _, kwargs = publish_single.call_args
    assert publish_single.call_args.args[0] == "/TEF/CPM-TESTE-2/cmd"
    assert kwargs["payload"] == "CPM-TESTE-2@paid|cmd=cmd-2|amount=5.00|"


def test_device_liberado_is_not_final_confirmation():
    assert _status_to_pulse_status("LIBERADO") == "pulso_enviado"
    assert "pulso_enviado" not in FINAL_PULSE_STATUSES
    assert _status_to_pulse_status("PULSO_CONFIRMADO") == "pulso_unitario"
    assert "pulso_unitario" not in FINAL_PULSE_STATUSES
    assert _status_to_pulse_status("PULSOS_CONCLUIDOS") == "pulso_confirmado"
    assert _status_to_pulse_status("PULSOS_ENVIADOS_SEM_RETORNO") == "pulso_confirmado"
    assert "pulso_confirmado" in FINAL_PULSE_STATUSES
    assert _status_to_pulse_status("PULSO_NAO_CONFIRMADO") == "pulso_sem_retorno"
    assert "pulso_sem_retorno" not in FINAL_PULSE_STATUSES

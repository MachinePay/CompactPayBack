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

from app.services.mqtt_commands import publish_machine_credit


def test_publish_machine_credit_uses_expected_topic_payload_and_qos():
    with patch("app.services.mqtt_commands.publish.single") as publish_single:
        payload = publish_machine_credit("CPM-TESTE", action="paid", command_id="cmd-1")

    assert payload == "CPM-TESTE@paid|cmd=cmd-1|"
    publish_single.assert_called_once()
    _, kwargs = publish_single.call_args
    assert publish_single.call_args.args[0] == "/TEF/CPM-TESTE/cmd"
    assert kwargs["payload"] == "CPM-TESTE@paid|cmd=cmd-1|"
    assert kwargs["qos"] == 1

import paho.mqtt.publish as publish
import time

from app.core.config import settings


def publish_machine_credit(machine_id: str, action: str = "paid") -> str:
    topic = f"/TEF/{machine_id}/cmd"
    payload = f"{machine_id}@{action.lower()}|"

    auth = None
    if getattr(settings, "MQTT_USERNAME", None):
        auth = {
            "username": settings.MQTT_USERNAME,
            "password": settings.MQTT_PASSWORD,
        }

    publish.single(
        topic,
        payload=payload,
        hostname=settings.MQTT_BROKER_URL,
        port=int(settings.MQTT_BROKER_PORT),
        auth=auth,
    )
    return payload


def publish_machine_credit_pulses(
    machine_id: str,
    pulses: int,
    action: str = "paid",
    interval_ms: int = 350,
) -> str:
    pulses_count = max(1, int(pulses))
    last_payload = ""
    for idx in range(pulses_count):
        last_payload = publish_machine_credit(machine_id=machine_id, action=action)
        if idx < pulses_count - 1 and interval_ms > 0:
            time.sleep(interval_ms / 1000)
    return last_payload

import paho.mqtt.publish as publish

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

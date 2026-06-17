import paho.mqtt.publish as publish
import logging

from app.core.config import settings


def publish_machine_credit(
    machine_id: str,
    action: str = "paid",
    command_id: str | None = None,
    pulses: int | None = None,
    amount: float | None = None,
) -> str:
    topic = f"/TEF/{machine_id}/cmd"
    payload = f"{machine_id}@{action.lower()}|"
    if command_id:
        payload += f"cmd={command_id}|"
    if pulses is not None:
        payload += f"pulses={max(1, int(pulses))}|"
    if amount is not None:
        payload += f"amount={float(amount):.2f}|"

    auth = None
    if getattr(settings, "MQTT_USERNAME", None):
        auth = {
            "username": settings.MQTT_USERNAME,
            "password": settings.MQTT_PASSWORD,
        }

    publish.single(
        topic,
        payload=payload,
        qos=int(settings.MQTT_COMMAND_QOS),
        hostname=settings.MQTT_BROKER_URL,
        port=int(settings.MQTT_BROKER_PORT),
        auth=auth,
    )
    logging.info("MQTT comando publicado machine_id=%s topic=%s payload=%s qos=%s", machine_id, topic, payload, settings.MQTT_COMMAND_QOS)
    return payload


def publish_machine_credit_pulses(
    machine_id: str,
    pulses: int,
    action: str = "paid",
    interval_ms: int = 350,
    command_id: str | None = None,
    amount: float | None = None,
) -> str:
    pulses_count = max(1, int(pulses))
    pulses_payload = None if amount is not None else pulses_count
    return publish_machine_credit(
        machine_id=machine_id,
        action=action,
        command_id=command_id,
        pulses=pulses_payload,
        amount=amount,
    )


def publish_machine_update(
    machine_id: str,
    firmware_url: str,
    command_id: str | None = None,
) -> str:
    topic = f"/TEF/{machine_id}/cmd"
    payload = f"{machine_id}@update|"
    if command_id:
        payload += f"cmd={command_id}|"
    payload += f"url={firmware_url}|"

    auth = None
    if getattr(settings, "MQTT_USERNAME", None):
        auth = {
            "username": settings.MQTT_USERNAME,
            "password": settings.MQTT_PASSWORD,
        }

    publish.single(
        topic,
        payload=payload,
        qos=int(settings.MQTT_COMMAND_QOS),
        hostname=settings.MQTT_BROKER_URL,
        port=int(settings.MQTT_BROKER_PORT),
        auth=auth,
    )
    logging.info("MQTT update publicado machine_id=%s topic=%s payload=%s qos=%s", machine_id, topic, payload, settings.MQTT_COMMAND_QOS)
    return payload

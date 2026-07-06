import paho.mqtt.publish as publish
import logging

from app.core.config import settings


def _mqtt_auth():
    if getattr(settings, "MQTT_USERNAME", None):
        return {
            "username": settings.MQTT_USERNAME,
            "password": settings.MQTT_PASSWORD,
        }
    return None


def publish_raw_mqtt_command(topic: str, payload: str) -> None:
    publish.single(
        topic,
        payload=payload,
        qos=int(settings.MQTT_COMMAND_QOS),
        hostname=settings.MQTT_BROKER_URL,
        port=int(settings.MQTT_BROKER_PORT),
        auth=_mqtt_auth(),
    )


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

    from app.services.command_queue import track_and_publish_command

    track_and_publish_command(
        machine_id=machine_id,
        command_id=command_id,
        tipo=action.lower(),
        topic=topic,
        payload=payload,
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
    firmware_version: str | None = None,
    command_id: str | None = None,
) -> str:
    topic = f"/TEF/{machine_id}/cmd"
    payload = f"{machine_id}@update|"
    if command_id:
        payload += f"cmd={command_id}|"
    if firmware_version:
        payload += f"version={firmware_version}|"
    payload += f"url={firmware_url}|"

    from app.services.command_queue import track_and_publish_command

    track_and_publish_command(
        machine_id=machine_id,
        command_id=command_id,
        tipo="update",
        topic=topic,
        payload=payload,
    )
    logging.info("MQTT update publicado machine_id=%s topic=%s payload=%s qos=%s", machine_id, topic, payload, settings.MQTT_COMMAND_QOS)
    return payload


def publish_machine_ping(machine_id: str, command_id: str) -> str:
    topic = f"/TEF/{machine_id}/cmd"
    payload = f"{machine_id}@ping|cmd={command_id}|"

    from app.services.command_queue import track_and_publish_command

    track_and_publish_command(
        machine_id=machine_id,
        command_id=command_id,
        tipo="ping",
        topic=topic,
        payload=payload,
    )
    logging.info("MQTT ping publicado machine_id=%s command_id=%s", machine_id, command_id)
    return payload

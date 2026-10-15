import paho.mqtt.publish as publish
import logging
from uuid import uuid4

from app.core.config import mqtt_tls_kwargs, settings


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
        client_id=f"{settings.MQTT_PUBLISH_CLIENT_ID_PREFIX}_{uuid4().hex[:12]}",
        auth=_mqtt_auth(),
        tls=mqtt_tls_kwargs(),
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


def publish_machine_device_config(
    machine_id: str,
    command_id: str,
    pulse_coin: str | None = None,
    pulse_out: str | None = None,
    pulse_credit: str | None = None,
    pulse_value: str | None = None,
    pulse_quantity: str | None = None,
    pulse_gap_ms: str | None = None,
    coin_debounce_us: str | None = None,
    wifi_hard_reset_ms: int | None = None,
    wifi_full_restart_ms: int | None = None,
) -> str:
    """Ajusta remotamente a configuracao de UMA maquina especifica - tudo que
    hoje so dava pra mudar reabrindo o portal fisico de Wi-Fi (pulso/moeda,
    debounce/liberacao do IN, tempos da escada de reconexao) EXCETO SSID/
    senha de Wi-Fi, que continuam so pelo portal fisico. Cada campo None e'
    omitido da mensagem - a placa mantem o que ja tinha pra esse campo."""
    topic = f"/TEF/{machine_id}/cmd"
    payload = f"{machine_id}@config|cmd={command_id}|"
    if pulse_coin is not None:
        payload += f"pulse_coin={pulse_coin}|"
    if pulse_out is not None:
        payload += f"pulse_out={pulse_out}|"
    if pulse_credit is not None:
        payload += f"pulse_credit={pulse_credit}|"
    if pulse_value is not None:
        payload += f"pulse_value={pulse_value}|"
    if pulse_quantity is not None:
        payload += f"pulse_quantity={pulse_quantity}|"
    if pulse_gap_ms is not None:
        payload += f"pulse_gap_ms={pulse_gap_ms}|"
    if coin_debounce_us is not None:
        payload += f"coin_debounce_us={coin_debounce_us}|"
    if wifi_hard_reset_ms is not None:
        payload += f"wifi_hard_reset_ms={int(wifi_hard_reset_ms)}|"
    if wifi_full_restart_ms is not None:
        payload += f"wifi_full_restart_ms={int(wifi_full_restart_ms)}|"

    from app.services.command_queue import track_and_publish_command

    track_and_publish_command(
        machine_id=machine_id,
        command_id=command_id,
        tipo="config",
        topic=topic,
        payload=payload,
    )
    logging.info("MQTT config de dispositivo publicado machine_id=%s payload=%s", machine_id, payload)
    return payload


def publish_machine_wifi(machine_id: str, command_id: str, ssid: str, senha: str) -> None:
    """Troca de Wi-Fi pelo painel (firmware 2.8.0+). SSID e senha vao em hex
    (podem ter "|"). A placa confere a rede antes de largar a atual e volta
    sozinha para ela se a nova nao conectar. Nada da senha vai para o banco
    nem para o log."""
    topic = f"/TEF/{machine_id}/cmd"
    payload = (
        f"{machine_id}@wifi|cmd={command_id}|ssid_hex={ssid.encode('utf-8').hex()}"
        f"|pass_hex={senha.encode('utf-8').hex()}|"
    )
    from app.services.command_queue import track_and_publish_sensitive_command

    track_and_publish_sensitive_command(
        machine_id=machine_id,
        command_id=command_id,
        tipo="wifi",
        topic=topic,
        payload=payload,
        payload_registrado=f"{machine_id}@wifi|cmd={command_id}|ssid={ssid}|senha=***|",
    )
    logging.info("MQTT troca de Wi-Fi publicada machine_id=%s command_id=%s rede=%r", machine_id, command_id, ssid)


def publish_machine_wifi_scan(machine_id: str, command_id: str) -> None:
    """Pede a lista de redes Wi-Fi perto da maquina (STATUS|WIFI_REDES)."""
    from app.services.command_queue import track_and_publish_command

    track_and_publish_command(
        machine_id=machine_id,
        command_id=command_id,
        tipo="wifi_scan",
        topic=f"/TEF/{machine_id}/cmd",
        payload=f"{machine_id}@wifi_scan|cmd={command_id}|",
    )


def publish_machine_wifi_localizar(machine_id: str, command_id: str) -> None:
    """Pede para a placa mandar os roteadores Wi-Fi em volta (localizacao)."""
    from app.services.command_queue import track_and_publish_command

    track_and_publish_command(
        machine_id=machine_id,
        command_id=command_id,
        tipo="wifi_localizar",
        topic=f"/TEF/{machine_id}/cmd",
        payload=f"{machine_id}@wifi_localizar|cmd={command_id}|",
    )


def publish_machine_wifi_info_request(machine_id: str, command_id: str) -> None:
    """Pede para a placa reenviar a rede/senha atuais (STATUS|WIFI_INFO)."""
    topic = f"/TEF/{machine_id}/cmd"
    payload = f"{machine_id}@wifi_info|cmd={command_id}|"
    from app.services.command_queue import track_and_publish_command

    track_and_publish_command(
        machine_id=machine_id,
        command_id=command_id,
        tipo="wifi_info",
        topic=topic,
        payload=payload,
    )
    logging.info("MQTT pedido de WIFI_INFO publicado machine_id=%s command_id=%s", machine_id, command_id)


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

import paho.mqtt.client as mqtt
import json
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.models import HistoricoOperacao, Maquina, Transacao, EventoTipo, MetodoPagamento
from app.models.logs import Logs
from app.services.command_queue import update_command_from_device_status
from app.services.pulse_tracking import device_event_description, update_pulse_status
from app.services.vendas import registrar_venda_pagamento
from sqlalchemy.orm import Session
from datetime import datetime

TOPIC = "/TEF/+/attrs"


def _parse_status_payload(payload: str) -> tuple[str | None, dict[str, str]]:
    if not payload.startswith("STATUS|"):
        return None, {}
    parts = payload.split("|")
    status = parts[1] if len(parts) > 1 else ""
    fields = {}
    for part in parts[2:]:
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key.strip()] = value.strip()
    return status, fields


def _status_to_pulse_status(status: str) -> str | None:
    return {
        "CMD_RECEBIDO": "cmd_recebido",
        "CMD_DUPLICADO": "cmd_duplicado",
        "PULSO_INICIADO": "pulso_iniciado",
        "LIBERADO": "pulso_enviado",
        "PULSO_CONFIRMADO": "pulso_unitario",
        "PULSOS_CONCLUIDOS": "pulso_confirmado",
        "PULSOS_ENVIADOS_SEM_RETORNO": "pulso_confirmado",
        "PULSO_NAO_CONFIRMADO": "pulso_sem_retorno",
        "SALDO_PENDENTE": "saldo_pendente",
        "UPDATE_INICIADO": "update_iniciado",
        "UPDATE_OK": "update_ok",
        "UPDATE_SEM_NOVIDADE": "update_sem_novidade",
        "UPDATE_FALHOU": "update_falhou",
        "CMD_IGNORADO": "falha_cmd_ignorado",
        "PULSO_BLOQUEADO_SEGURANCA": "falha_bloqueado",
    }.get(status)


def _parse_int_field(fields: dict[str, str], key: str) -> int | None:
    value = fields.get(key)
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None

def on_connect(client, userdata, flags, rc):
    print(f"MQTT conectado com código {rc}")
    client.subscribe(TOPIC)

def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode()
        topic_parts = msg.topic.split('/')
        if len(topic_parts) >= 3:
            id_extraido = topic_parts[2]
        else:
            print("Tópico inválido")
            return
        db: Session = SessionLocal()
        maquina = db.query(Maquina).filter(Maquina.id_hardware == id_extraido).first()
        if not maquina:
            maquina = Maquina(id_hardware=id_extraido, nome_local="Desconhecido")
            db.add(maquina)
            db.commit()
            db.refresh(maquina)
        # Sempre que receber sinal, atualiza o timestamp do último sinal
        from datetime import datetime
        maquina.ultimo_sinal = datetime.utcnow()
        db.commit()
        status, status_fields = _parse_status_payload(payload)
        if status:
            command_id = status_fields.get("cmd")
            firmware_version = status_fields.get("fw")
            wifi_rssi = _parse_int_field(status_fields, "rssi")
            wifi_quality = _parse_int_field(status_fields, "wifi")
            if wifi_rssi is not None:
                maquina.wifi_rssi = wifi_rssi
            if wifi_quality is not None:
                maquina.wifi_quality = max(0, min(100, wifi_quality))
            if firmware_version:
                maquina.firmware_version = firmware_version
                maquina.firmware_updated_at = datetime.utcnow()
                if maquina.firmware_update_status in {"sent", "downloading", "restarting", "failed", "no_update"}:
                    maquina.firmware_update_status = "updated"
                    maquina.firmware_update_finished_at = datetime.utcnow()
                if maquina.firmware_target_version and maquina.firmware_target_version == firmware_version:
                    maquina.firmware_target_version = None
            if status == "UPDATE_INICIADO":
                maquina.firmware_update_status = "downloading"
                maquina.firmware_update_started_at = datetime.utcnow()
                if command_id:
                    maquina.firmware_update_command_id = command_id
                if status_fields.get("url"):
                    maquina.firmware_update_url = status_fields.get("url")
            elif status == "UPDATE_OK":
                maquina.firmware_update_status = "restarting"
                maquina.firmware_update_finished_at = datetime.utcnow()
            elif status == "UPDATE_SEM_NOVIDADE":
                maquina.firmware_update_status = "no_update"
                maquina.firmware_update_finished_at = datetime.utcnow()
            elif status == "UPDATE_FALHOU":
                maquina.firmware_update_status = "failed"
                maquina.firmware_update_finished_at = datetime.utcnow()
            pulse_status = _status_to_pulse_status(status)
            if command_id:
                update_command_from_device_status(command_id, status)
            if command_id and pulse_status:
                update_pulse_status(command_id, pulse_status)
            db.add(
                HistoricoOperacao(
                    maquina_id=id_extraido,
                    categoria="DISPOSITIVO",
                    descricao=device_event_description(
                        status,
                        command_id,
                        " ".join(f"{key}={value}" for key, value in status_fields.items() if key != "cmd") or None,
                    ),
                    valor=None,
                    command_id=command_id,
                    pulse_status=pulse_status,
                    created_at=datetime.utcnow(),
                )
            )
            db.commit()
            print(f"Status MQTT registrado para maquina {id_extraido}: {payload}")
            db.close()
            return
        # Processamento de pulso
        if payload == "MOEDA DETECTADA (IN)":
            nova_transacao = Transacao(
                maquina_id=id_extraido,
                tipo=EventoTipo.in_flux,
                metodo=MetodoPagamento.fisico,
                valor=1.00
            )
            db.add(nova_transacao)
            db.flush()
            registrar_venda_pagamento(
                db,
                maquina_id=id_extraido,
                valor=1.00,
                origem="fisico",
                transacao_id=nova_transacao.id,
                provider="fisico",
                tipo_pagamento="moeda_nota",
                status_pulso="fisico",
                created_at=nova_transacao.data_hora,
            )
            db.commit()
            print(f"Transação FISICO IN registrada para máquina {id_extraido}")
        elif payload == "PELUCIA ENTREGUE (OUT)":
            nova_transacao = Transacao(
                maquina_id=id_extraido,
                tipo=EventoTipo.out_flux,
                metodo=MetodoPagamento.fisico,
                valor=0.0
            )
            db.add(nova_transacao)
            db.commit()
            print(f"Transação FISICO OUT registrada para máquina {id_extraido}")
        db.close()
    except Exception as e:
        print(f"Erro ao processar mensagem MQTT: {e}")
        # Grava erro no banco de dados (Logs)
        db = SessionLocal()
        log = Logs(
            message=str(e),
            level="ERROR"
        )
        db.add(log)
        db.commit()
        db.close()

def start_mqtt_worker():
    client = mqtt.Client()
    if getattr(settings, "MQTT_USERNAME", None):
        client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(settings.MQTT_BROKER_URL, int(settings.MQTT_BROKER_PORT), 60)
    client.loop_forever()

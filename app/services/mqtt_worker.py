import paho.mqtt.client as mqtt
import json
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.models import Maquina, Transacao, EventoTipo, MetodoPagamento
from app.models.logs import Logs
from sqlalchemy.orm import Session
from datetime import datetime

TOPIC = "/TEF/+/attrs"

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
        # Processamento de pulso
        if payload == "MOEDA DETECTADA (IN)":
            nova_transacao = Transacao(
                maquina_id=id_extraido,
                tipo=EventoTipo.in_flux,
                metodo=MetodoPagamento.fisico,
                valor=1.00
            )
            db.add(nova_transacao)
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
    client.connect(settings.MQTT_BROKER_HOST, int(settings.MQTT_BROKER_PORT), 60)
    client.loop_forever()

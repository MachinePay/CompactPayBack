import paho.mqtt.client as mqtt
import json
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.transacoes import Transacoes, TipoTransacao
from app.models.maquinas import Maquinas
from sqlalchemy.orm import Session
from datetime import datetime

def on_connect(client, userdata, flags, rc):
    print(f"MQTT conectado com código {rc}")
    client.subscribe("/TEF/+/attrs")

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        # Extrai o id da máquina do tópico
        topic_parts = msg.topic.split('/')
        if len(topic_parts) >= 3:
            machine_id = topic_parts[2]
        else:
            print("Tópico inválido")
            return
        valor = float(payload.get("valor", 0))
        tipo = payload.get("tipo", "IN")
        if tipo not in ("IN", "OUT"):
            tipo = "IN"
        db: Session = SessionLocal()
        transacao = Transacoes(
            machine_id=machine_id,
            valor=valor,
            tipo=TipoTransacao(tipo),
            timestamp=datetime.utcnow()
        )
        db.add(transacao)
        # Atualiza faturamento_total da máquina
        maquina = db.query(Maquinas).filter(Maquinas.id_unico == machine_id).first()
        if maquina:
            if tipo == "IN":
                maquina.faturamento_total += valor
            elif tipo == "OUT":
                maquina.faturamento_total -= valor
        db.commit()
        db.close()
        print(f"Transação registrada para máquina {machine_id}")
    except Exception as e:
        print(f"Erro ao processar mensagem MQTT: {e}")

def start_mqtt_worker():
    client = mqtt.Client()
    if settings.MQTT_USERNAME:
        client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(settings.MQTT_BROKER_URL, settings.MQTT_BROKER_PORT, 60)
    client.loop_forever()

from fastapi import APIRouter, Request
from app.db.session import SessionLocal
from app.models.models import Transacao, EventoTipo, MetodoPagamento
from datetime import datetime
#import paho.mqtt.publish as publish  # Para uso futuro

router = APIRouter()

@router.post("/callback-mercado-pago")
async def processar_pix(dados: dict):
    # 1. Valida se o Pix foi pago (mock/futuro)
    pago = dados.get("status") == "approved"
    id_hardware = dados.get("id_hardware")
    valor = float(dados.get("valor", 1.0))
    if not (pago and id_hardware):
        return {"status": "erro", "detalhe": "Pix não aprovado ou máquina não informada"}
    db = SessionLocal()
    nova_transacao = Transacao(
        maquina_id=id_hardware,
        tipo=EventoTipo.in_flux,
        metodo=MetodoPagamento.digital,
        valor=valor,
        data_hora=datetime.utcnow()
    )
    db.add(nova_transacao)
    db.commit()
    db.close()
    # 3. Envia comando MQTT para a máquina (mock/futuro)
    # publish.single(f"/TEF/{id_hardware}/cmd", f"{id_hardware}@paid|", hostname="52.14.249.201")
    return {"status": "sucesso, pulso enviado"}

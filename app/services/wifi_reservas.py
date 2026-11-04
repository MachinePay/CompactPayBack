"""Envio das redes Wi-Fi reserva para a placa (rodizio, firmware 2.9.0+).

O painel salva a lista a qualquer hora, mesmo com a maquina offline ou com
firmware antigo: fica maquina.wifi_reservas_pendente = True. A placa recebe
assim que estiver online com firmware que suporta - na hora, se ja estiver,
ou pelo worker MQTT no proximo sinal dela. A pendencia so sai quando a placa
responde WIFI_RESERVAS_SALVAS (ou RECUSADAS) ao ultimo envio.
"""
import logging
import threading
from datetime import datetime, timedelta
from uuid import uuid4

from app.models.models import ComandoMaquina, Maquina, MaquinaWifiReserva
from app.services.maquinas_relatorio import ONLINE_SIGNAL_WINDOW
from app.services.wifi_credenciais import descriptografar_senha

MIN_FIRMWARE_VERSION_FOR_RESERVAS = (2, 9, 0)
# Espera entre reenvios enquanto a placa nao confirma (comando sensivel nao
# tem retry automatico na fila).
REENVIO_APOS = timedelta(minutes=2)


def suporta_reservas(firmware_version: str | None) -> bool:
    from app.api.v1.endpoints.maquinas_operacoes import _firmware_version_tuple

    versao = _firmware_version_tuple(firmware_version)
    return versao is not None and versao >= MIN_FIRMWARE_VERSION_FOR_RESERVAS


def pode_enviar_agora(maquina: Maquina) -> bool:
    online = bool(maquina.ultimo_sinal) and datetime.utcnow() - maquina.ultimo_sinal <= ONLINE_SIGNAL_WINDOW
    return online and suporta_reservas(maquina.firmware_version)


def ultimo_envio(db, machine_id: str) -> ComandoMaquina | None:
    return (
        db.query(ComandoMaquina)
        .filter(ComandoMaquina.maquina_id == machine_id, ComandoMaquina.tipo == "wifi_reservas")
        .order_by(ComandoMaquina.created_at.desc(), ComandoMaquina.id.desc())
        .first()
    )


def redes_para_envio(db, machine_id: str) -> list[tuple[str, str]] | None:
    """Lista (ssid, senha) na ordem; None se alguma senha ficou ilegivel."""
    redes = []
    for reserva in (
        db.query(MaquinaWifiReserva)
        .filter(MaquinaWifiReserva.maquina_id == machine_id)
        .order_by(MaquinaWifiReserva.ordem)
        .all()
    ):
        senha = descriptografar_senha(reserva.senha_cript)
        if senha is None:
            return None
        redes.append((reserva.ssid, senha))
    return redes


def _em_segundo_plano(funcao) -> None:
    threading.Thread(target=funcao, daemon=True).start()


def enviar_reservas_pendentes(db, maquina: Maquina, em_segundo_plano: bool = True) -> str | None:
    """Manda a lista se houver pendencia e der para enviar. Retorna o
    command_id do envio, ou None se nao enviou."""
    if not maquina.wifi_reservas_pendente or not pode_enviar_agora(maquina):
        return None
    anterior = ultimo_envio(db, maquina.id_hardware)
    if anterior and anterior.status != "executado" and datetime.utcnow() - anterior.created_at < REENVIO_APOS:
        # Esperando a resposta do envio anterior, ou ele falhou ha pouco
        # (sem ACK): nao reenvia a cada heartbeat.
        return None
    redes = redes_para_envio(db, maquina.id_hardware)
    if redes is None:
        logging.warning("Redes reserva com senha ilegivel, nao enviadas machine_id=%s", maquina.id_hardware)
        return None

    from app.services.mqtt_commands import publish_machine_wifi_reservas

    command_id = str(uuid4())
    if not em_segundo_plano:
        publish_machine_wifi_reservas(maquina.id_hardware, command_id, redes)
        return command_id

    def _publicar():
        try:
            publish_machine_wifi_reservas(maquina_id, command_id, redes)
        except Exception:
            logging.exception("Falha ao enviar redes reserva machine_id=%s", maquina_id)

    maquina_id = maquina.id_hardware
    # Abrir conexao MQTT para publicar leva ~1s: fora da thread do worker.
    _em_segundo_plano(_publicar)
    return command_id


def concluir_envio(db, maquina: Maquina, command_id: str | None) -> None:
    """Placa respondeu WIFI_RESERVAS_SALVAS/RECUSADAS: tira a pendencia se for
    a resposta ao ultimo envio (lista salva depois ainda precisa ir)."""
    if not command_id:
        return
    anterior = ultimo_envio(db, maquina.id_hardware)
    if anterior and anterior.command_id == command_id:
        maquina.wifi_reservas_pendente = False

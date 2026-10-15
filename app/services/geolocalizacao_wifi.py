"""Localizacao da maquina pelos roteadores Wi-Fi em volta (Google Geolocation API).

A placa (firmware 2.8.5+) manda STATUS|WIFI_LOCALIZACAO|aps=bssid,rssi,canal;...
depois do portal dos 5 cliques ou quando o admin clica "Localizar pela Wi-Fi".
O servidor pergunta ao Google onde ficam esses roteadores.

- Chave: variavel de ambiente GOOGLE_GEOLOCATION_API_KEY. Sem ela nada e'
  consultado (status "sem_chave").
- No maximo 1 consulta a cada 10 min por maquina (custo).
- Nao troca uma posicao mais precisa no mesmo lugar (ex.: GPS do celular):
  so atualiza se nao havia posicao, se a nova e' mais precisa, ou se o ponto
  novo esta longe do atual (a maquina mudou de lugar).
- Roda fora do worker MQTT (thread), para nao atrasar pagamentos.
"""
import logging
import math
import os
import threading
from datetime import datetime, timedelta

import httpx

from app.db.session import SessionLocal
from app.models.models import HistoricoOperacao, Maquina

GOOGLE_URL = "https://www.googleapis.com/geolocation/v1/geolocate"
INTERVALO_MINIMO = timedelta(minutes=10)
MIN_ROTEADORES = 2
TIMEOUT_S = 8.0
# Ponto novo a ate isso (ou dentro da precisao) do atual = mesmo lugar.
MESMO_LUGAR_M = 100.0


def interpretar_roteadores(texto: str | None) -> list[dict]:
    """ "aabbccddeeff,-60,6;..." -> formato wifiAccessPoints do Google."""
    roteadores = []
    for item in (texto or "").split(";"):
        partes = item.split(",")
        if len(partes) != 3 or len(partes[0]) != 12:
            continue
        try:
            int(partes[0], 16)
            rssi = int(partes[1])
            canal = int(partes[2])
        except ValueError:
            continue
        mac = ":".join(partes[0][i:i + 2] for i in range(0, 12, 2)).lower()
        roteadores.append({"macAddress": mac, "signalStrength": rssi, "channel": canal})
    return roteadores


def distancia_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    raio = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * raio * math.asin(math.sqrt(a))


def consultar_google(roteadores: list[dict], chave: str) -> tuple[float, float, float] | None:
    resposta = httpx.post(
        GOOGLE_URL,
        params={"key": chave},
        json={"considerIp": False, "wifiAccessPoints": roteadores},
        timeout=TIMEOUT_S,
    )
    if resposta.status_code == 404:
        return None  # Google nao achou posicao para esses roteadores
    resposta.raise_for_status()
    dados = resposta.json()
    local = dados.get("location") or {}
    if "lat" not in local or "lng" not in local:
        return None
    return float(local["lat"]), float(local["lng"]), float(dados.get("accuracy") or 0)


def _deve_atualizar(maquina: Maquina, lat: float, lng: float, precisao: float) -> bool:
    if maquina.latitude is None or maquina.longitude is None:
        return True
    distancia = distancia_m(maquina.latitude, maquina.longitude, lat, lng)
    if distancia > max(MESMO_LUGAR_M, precisao):
        return True  # mudou de lugar
    precisao_atual = maquina.localizacao_precisao_m
    return precisao_atual is not None and precisao < precisao_atual


def localizar_por_wifi(machine_id: str, texto_roteadores: str | None, agora: datetime | None = None) -> str:
    """Processa uma varredura. Devolve o status gravado na maquina."""
    agora = agora or datetime.utcnow()
    db = SessionLocal()
    try:
        maquina = db.query(Maquina).filter(Maquina.id_hardware == machine_id).first()
        if not maquina:
            return "maquina_inexistente"
        if maquina.localizacao_wifi_em and agora - maquina.localizacao_wifi_em < INTERVALO_MINIMO:
            return "limite"  # nao grava: mantem o resultado anterior
        maquina.localizacao_wifi_em = agora

        status = "sem_redes"
        roteadores = interpretar_roteadores(texto_roteadores)
        chave = os.getenv("GOOGLE_GEOLOCATION_API_KEY", "").strip()
        if not chave:
            status = "sem_chave"
        elif len(roteadores) >= MIN_ROTEADORES:
            try:
                resultado = consultar_google(roteadores, chave)
            except httpx.HTTPError:
                logging.exception("Falha na Geolocation API para a maquina %s", machine_id)
                resultado = None
                status = "erro"
            if resultado:
                lat, lng, precisao = resultado
                if _deve_atualizar(maquina, lat, lng, precisao):
                    maquina.latitude, maquina.longitude = lat, lng
                    maquina.localizacao_origem = "wifi"
                    maquina.localizacao_precisao_m = precisao
                    maquina.localizacao_atualizada_em = agora
                    status = "ok"
                    db.add(
                        HistoricoOperacao(
                            maquina_id=machine_id,
                            categoria="DISPOSITIVO",
                            descricao=f"Localizacao encontrada pela Wi-Fi (+-{int(precisao)} m)",
                            created_at=agora,
                        )
                    )
                else:
                    status = "mantida"  # ja tinha posicao igual ou melhor no mesmo lugar
            elif status != "erro":
                status = "nao_encontrada"
        maquina.localizacao_wifi_status = status
        db.commit()
        return status
    finally:
        db.close()


def localizar_por_wifi_em_segundo_plano(machine_id: str, texto_roteadores: str | None) -> None:
    threading.Thread(
        target=localizar_por_wifi, args=(machine_id, texto_roteadores), name=f"geo-wifi-{machine_id}", daemon=True
    ).start()

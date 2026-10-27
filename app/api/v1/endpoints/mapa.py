"""Mapa das maquinas.

Acesso pela regra de app/core/escopo.py: admin ve todas; cliente ve SO as
proprias maquinas. So aparece no mapa a maquina com localizacao real
cadastrada; as outras vao para a lista "sem localizacao", que so o admin ve
(cadastrar/alterar localizacao e' so admin).
"""
import re
from datetime import datetime, timedelta
from urllib.parse import unquote, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.core.dependencies import get_current_user
from app.core.escopo import maquinas_do_usuario, obter_maquina_do_usuario
from app.db.session import SessionLocal
from app.models.models import Cliente, Maquina
from app.services.auditoria import registrar_auditoria
from app.services.horario_fechado import maquina_fechada
from app.services.maquinas_relatorio import (
    ONLINE_SIGNAL_WINDOW,
    compute_financial_summary_by_machine,
    latest_payment_by_machine,
    resolve_date_window,
)

router = APIRouter()

# Links curtos do Google Maps que precisam ser abertos para achar as
# coordenadas. So esses hosts sao seguidos (nunca um endereco qualquer).
HOSTS_LINK_CURTO = {"maps.app.goo.gl", "goo.gl", "g.co"}
# GPS pior que isso (comum dentro de predio) nao sobrescreve a posicao.
GPS_PRECISAO_MAXIMA_M = 150.0
PADROES_COORDENADAS = [
    re.compile(r"!3d(-?\d{1,3}\.\d+)!4d(-?\d{1,3}\.\d+)"),
    re.compile(r"@(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)"),
    re.compile(r"[?&](?:q|query|ll|destination)=(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)"),
    re.compile(r"^\s*(-?\d{1,3}\.\d+)\s*[,;\s]\s*(-?\d{1,3}\.\d+)\s*$"),
]


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _exigir_admin(user) -> None:
    if user[1] != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode fazer isso")


def extrair_coordenadas(texto: str) -> tuple[float, float] | None:
    """Coordenadas de "lat, lng" ou de um link do Google Maps (inclusive link
    curto maps.app.goo.gl, que e' aberto para achar o endereco final)."""
    texto = unquote((texto or "").strip())
    if not texto:
        return None
    candidatos = [texto]
    if texto.startswith("http"):
        host = (urlparse(texto).hostname or "").lower()
        if host in HOSTS_LINK_CURTO:
            try:
                resposta = httpx.get(texto, follow_redirects=True, timeout=6.0)
                candidatos.append(unquote(str(resposta.url)))
                candidatos.append(unquote(resposta.text[:20000]))
            except httpx.HTTPError:
                pass
    for candidato in candidatos:
        for padrao in PADROES_COORDENADAS:
            achou = padrao.search(candidato)
            if achou:
                lat, lng = float(achou.group(1)), float(achou.group(2))
                if -90 <= lat <= 90 and -180 <= lng <= 180:
                    return lat, lng
    return None


@router.get("/mapa/acesso")
def acesso_mapa(user=Depends(get_current_user)):
    # Mapa liberado para todo login (cada um ve so as maquinas que pode ver).
    return {"permitido": True}


@router.get("/mapa/maquinas")
def maquinas_no_mapa(
    cliente_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    role = user[1]
    # Regra central: cliente so ve as proprias maquinas (cliente_id e' ignorado).
    maquinas = maquinas_do_usuario(db, user, cliente_id)
    ids = [m.id_hardware for m in maquinas]
    inicio, fim = resolve_date_window("dia")
    financeiro = compute_financial_summary_by_machine(db, ids, inicio, fim) if ids else {}
    ultimos = latest_payment_by_machine(db, ids) if ids else {}
    agora = datetime.utcnow()

    no_mapa = []
    sem_localizacao = []
    for maquina in maquinas:
        dono = maquina.dono
        lat, lng = maquina.latitude, maquina.longitude
        online = bool(maquina.ultimo_sinal and agora - maquina.ultimo_sinal < ONLINE_SIGNAL_WINDOW)
        item = {
            "maquina_id": maquina.id_hardware,
            "maquina_nome": maquina.nome_local or maquina.id_hardware,
            "local": maquina.localizacao,
            "cliente_nome": dono.nome_empresa if dono else None,
            "online": online,
            "fechado": maquina_fechada(maquina, online, agora),
            "wifi_quality": maquina.wifi_quality,
            "faturamento_hoje": round(float((financeiro.get(maquina.id_hardware) or {}).get("faturamento_total") or 0), 2),
            "ultima_venda_em": (ultimos.get(maquina.id_hardware) or {}).get("data"),
        }
        if lat is None or lng is None:
            sem_localizacao.append(item)
        else:
            no_mapa.append(
                {
                    **item,
                    "latitude": lat,
                    "longitude": lng,
                    "localizacao_origem": maquina.localizacao_origem,
                    "localizacao_precisao_m": maquina.localizacao_precisao_m,
                    "localizacao_atualizada_em": maquina.localizacao_atualizada_em,
                }
            )
    return {"maquinas": no_mapa, "sem_localizacao": sem_localizacao if role == "admin" else []}


@router.put("/mapa/maquinas/{machine_id}/localizacao")
def definir_localizacao(machine_id: str, payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """payload: {"texto": "lat, lng" ou link do Google Maps}, {"limpar": true}
    ou {"latitude", "longitude", "precisao_m"} vindo do GPS do celular (o painel
    manda isso sozinho quando a maquina e' configurada pelo celular)."""
    _exigir_admin(user)
    maquina = db.query(Maquina).filter(Maquina.id_hardware == machine_id).first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")
    agora = datetime.utcnow()
    if payload.get("limpar"):
        maquina.latitude = None
        maquina.longitude = None
        maquina.localizacao_origem = None
        maquina.localizacao_precisao_m = None
        maquina.localizacao_atualizada_em = agora
        descricao = f"Removeu a localizacao da maquina {machine_id} no mapa"
    elif payload.get("latitude") is not None and payload.get("longitude") is not None:
        try:
            lat = float(payload["latitude"])
            lng = float(payload["longitude"])
            precisao = float(payload["precisao_m"]) if payload.get("precisao_m") is not None else None
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Localizacao do GPS invalida") from None
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise HTTPException(status_code=422, detail="Localizacao do GPS invalida")
        if precisao is not None and precisao > GPS_PRECISAO_MAXIMA_M:
            raise HTTPException(
                status_code=422,
                detail=f"GPS do celular impreciso (+-{int(precisao)} m). Tente ao ar livre ou perto de uma janela.",
            )
        maquina.latitude, maquina.longitude = lat, lng
        maquina.localizacao_origem = "celular"
        maquina.localizacao_precisao_m = precisao
        maquina.localizacao_atualizada_em = agora
        descricao = f"Localizacao da maquina {machine_id} pelo GPS do celular ({lat:.5f}, {lng:.5f}, +-{int(precisao or 0)} m)"
    else:
        coordenadas = extrair_coordenadas(str(payload.get("texto") or ""))
        if not coordenadas:
            raise HTTPException(
                status_code=422,
                detail="Nao achei a localizacao. Cole o link do Google Maps do ponto ou as coordenadas (ex.: -23.5505, -46.6333).",
            )
        maquina.latitude, maquina.longitude = coordenadas
        maquina.localizacao_origem = "manual"
        maquina.localizacao_precisao_m = None
        maquina.localizacao_atualizada_em = agora
        descricao = f"Definiu a localizacao da maquina {machine_id} no mapa ({coordenadas[0]:.5f}, {coordenadas[1]:.5f})"
    registrar_auditoria(db, user, "mapa_localizacao", "maquina", machine_id, descricao)
    db.commit()
    return {"latitude": maquina.latitude, "longitude": maquina.longitude}


AVISO_LOCALIZACAO_JANELA = timedelta(minutes=15)
MIN_FIRMWARE_LOCALIZAR_WIFI = (2, 8, 5)


@router.post("/mapa/maquinas/{machine_id}/localizar-wifi")
def localizar_pela_wifi(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Pede para a placa varrer os roteadores em volta; o servidor consulta o
    Google e atualiza a posicao (resultado em GET .../localizacao)."""
    _exigir_admin(user)
    from uuid import uuid4

    from app.api.v1.endpoints.maquinas_operacoes import _firmware_version_tuple
    from app.services.mqtt_commands import publish_machine_wifi_localizar

    maquina = obter_maquina_do_usuario(db, user, machine_id)
    versao = _firmware_version_tuple(maquina.firmware_version)
    if versao is None or versao < MIN_FIRMWARE_LOCALIZAR_WIFI:
        raise HTTPException(
            status_code=409,
            detail=f"Firmware {maquina.firmware_version or 'desconhecido'}: atualize para 2.8.5 ou mais novo para localizar pela Wi-Fi.",
        )
    if not maquina.ultimo_sinal or datetime.utcnow() - maquina.ultimo_sinal > ONLINE_SIGNAL_WINDOW:
        raise HTTPException(status_code=409, detail="A maquina precisa estar online para isso")
    command_id = str(uuid4())
    publish_machine_wifi_localizar(machine_id, command_id)
    return {"command_id": command_id, "pedido_em": datetime.utcnow()}


@router.get("/mapa/maquinas/{machine_id}/localizacao")
def localizacao_da_maquina(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    maquina = obter_maquina_do_usuario(db, user, machine_id)
    return {
        "latitude": maquina.latitude,
        "longitude": maquina.longitude,
        "origem": maquina.localizacao_origem,
        "precisao_m": maquina.localizacao_precisao_m,
        "atualizada_em": maquina.localizacao_atualizada_em,
        "wifi_status": maquina.localizacao_wifi_status,
        "wifi_em": maquina.localizacao_wifi_em,
    }


@router.get("/mapa/configuradas-agora")
def maquinas_configuradas_agora(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Maquinas configuradas no local (portal dos 5 cliques) nos ultimos 15
    min e cuja localizacao nao foi atualizada depois disso. O painel no
    celular oferece salvar a posicao pelo GPS."""
    _exigir_admin(user)
    limite = datetime.utcnow() - AVISO_LOCALIZACAO_JANELA
    maquinas = (
        db.query(Maquina)
        .options(joinedload(Maquina.dono))
        .filter(Maquina.portal_configurada_em.isnot(None), Maquina.portal_configurada_em >= limite)
        .order_by(Maquina.portal_configurada_em.desc())
        .all()
    )
    return [
        {
            "maquina_id": m.id_hardware,
            "maquina_nome": m.nome_local or m.id_hardware,
            "cliente_nome": m.dono.nome_empresa if m.dono else None,
            "configurada_em": m.portal_configurada_em,
            "ja_tem_localizacao": m.latitude is not None,
        }
        for m in maquinas
        if not m.localizacao_atualizada_em or m.localizacao_atualizada_em < m.portal_configurada_em
    ]

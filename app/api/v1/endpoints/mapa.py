"""Mapa das maquinas.

Admin sempre ve. Cliente so ve se o admin ativar (Cliente.mapa_habilitado,
desligado por padrao). So aparece no mapa a maquina com localizacao real
cadastrada (latitude/longitude da propria maquina); as outras vao para a
lista "sem localizacao", que so o admin ve (para cadastrar).
"""
import re
from datetime import datetime
from urllib.parse import unquote, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import Cliente, Maquina
from app.services.auditoria import registrar_auditoria
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


def _mapa_permitido(db: Session, user) -> bool:
    _, role, cliente_id = user
    if role == "admin":
        return True
    cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    return bool(cliente and cliente.mapa_habilitado)


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
def acesso_mapa(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return {"permitido": _mapa_permitido(db, user)}


@router.get("/mapa/maquinas")
def maquinas_no_mapa(
    cliente_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not _mapa_permitido(db, user):
        raise HTTPException(status_code=403, detail="Mapa nao liberado para esta conta")
    _, role, cliente_do_usuario = user
    query = db.query(Maquina).options(joinedload(Maquina.dono))
    if role != "admin":
        query = query.filter(Maquina.cliente_id == cliente_do_usuario)
    elif cliente_id is not None:
        query = query.filter(Maquina.cliente_id == cliente_id)
    maquinas = query.all()
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
            "wifi_quality": maquina.wifi_quality,
            "faturamento_hoje": round(float((financeiro.get(maquina.id_hardware) or {}).get("faturamento_total") or 0), 2),
            "ultima_venda_em": (ultimos.get(maquina.id_hardware) or {}).get("data"),
        }
        if lat is None or lng is None:
            sem_localizacao.append(item)
        else:
            no_mapa.append({**item, "latitude": lat, "longitude": lng})
    return {"maquinas": no_mapa, "sem_localizacao": sem_localizacao if role == "admin" else []}


@router.put("/mapa/maquinas/{machine_id}/localizacao")
def definir_localizacao(machine_id: str, payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """payload: {"texto": "lat, lng" ou link do Google Maps} ou {"limpar": true}."""
    _exigir_admin(user)
    maquina = db.query(Maquina).filter(Maquina.id_hardware == machine_id).first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")
    if payload.get("limpar"):
        maquina.latitude = None
        maquina.longitude = None
        descricao = f"Removeu a localizacao da maquina {machine_id} no mapa"
    else:
        coordenadas = extrair_coordenadas(str(payload.get("texto") or ""))
        if not coordenadas:
            raise HTTPException(
                status_code=422,
                detail="Nao achei a localizacao. Cole o link do Google Maps do ponto ou as coordenadas (ex.: -23.5505, -46.6333).",
            )
        maquina.latitude, maquina.longitude = coordenadas
        descricao = f"Definiu a localizacao da maquina {machine_id} no mapa ({coordenadas[0]:.5f}, {coordenadas[1]:.5f})"
    registrar_auditoria(db, user, "mapa_localizacao", "maquina", machine_id, descricao)
    db.commit()
    return {"latitude": maquina.latitude, "longitude": maquina.longitude}


@router.get("/mapa/clientes")
def clientes_acesso_mapa(db: Session = Depends(get_db), user=Depends(get_current_user)):
    _exigir_admin(user)
    return [
        {"id": c.id, "nome": c.nome_empresa or c.email_contato, "mapa_habilitado": bool(c.mapa_habilitado)}
        for c in db.query(Cliente).order_by(Cliente.nome_empresa).all()
    ]


@router.put("/mapa/clientes/{cliente_id}")
def alterar_acesso_mapa(cliente_id: int, payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _exigir_admin(user)
    cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    cliente.mapa_habilitado = bool(payload.get("habilitado"))
    registrar_auditoria(
        db,
        user,
        "mapa_acesso_cliente",
        "cliente",
        cliente_id,
        f"{'Liberou' if cliente.mapa_habilitado else 'Bloqueou'} o mapa para o cliente {cliente.nome_empresa}",
    )
    db.commit()
    return {"id": cliente.id, "mapa_habilitado": cliente.mapa_habilitado}

from datetime import datetime, timedelta
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import (
    AuditoriaOperacao,
    Cliente,
    EscutaTerminal,
    FechamentoMaquina,
    HistoricoOperacao,
    Maquina,
    Transacao,
    VendaPagamento,
)
from app.models.produto import Produto
from app.schemas.maquina import MaquinaCreate, MaquinaOut, MaquinaUpdate
from app.services.auditoria import registrar_auditoria
from app.services.maquinas_relatorio import serialize_machine_summary
from app.services.mercado_pago import create_pos_for_machine

router = APIRouter()
ONLINE_SIGNAL_WINDOW = timedelta(seconds=90)
PULSE_ABSENT_STATUSES = {
    "falha",
    "falha_timeout",
    "falha_sem_confirmacao",
    "falha_publicacao",
    "falha_cmd_ignorado",
    "falha_bloqueado",
    "pulso_sem_retorno",
}
OTA_ACTIVE_STATUSES = {"sent", "downloading", "restarting"}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _maquina_query_por_usuario(db: Session, role: str, cliente_id):
    if role == "admin":
        return db.query(Maquina)
    return db.query(Maquina).filter(Maquina.cliente_id == cliente_id)


def _generate_machine_id(db: Session) -> str:
    numeric_ids = []
    for (id_hardware,) in db.query(Maquina.id_hardware).all():
        value = str(id_hardware or "").strip()
        if value.isdigit():
            numeric_ids.append(int(value))

    next_id = max(numeric_ids, default=999) + 1
    next_id = max(next_id, 1000)
    while db.query(Maquina).filter(Maquina.id_hardware == str(next_id)).first():
        next_id += 1
    return str(next_id)


def _get_maquina_visivel(db: Session, machine_id: str, role: str, cliente_id):
    query = _maquina_query_por_usuario(db, role, cliente_id)
    maquina = query.filter(Maquina.id_hardware == machine_id).first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")
    return maquina


def _wifi_health(quality) -> str:
    if quality is None:
        return "sem_leitura"
    if quality >= 70:
        return "otimo"
    if quality >= 40:
        return "bom"
    return "ruim"


def _machine_health_status(status_online: bool, wifi_status: str, firmware_alert: bool, pulse_alert: bool) -> str:
    if not status_online:
        return "offline"
    if wifi_status == "ruim" or firmware_alert or pulse_alert:
        return "atencao"
    return "online"


def _latest_payment(db: Session, machine_id: str):
    venda = (
        db.query(VendaPagamento)
        .filter(VendaPagamento.maquina_id == machine_id)
        .order_by(VendaPagamento.created_at.desc())
        .first()
    )
    if venda:
        return {
            "data": venda.created_at,
            "valor": float(venda.valor_liquido or venda.valor_bruto or 0),
            "origem": venda.origem,
            "provider": venda.provider,
            "payment_type": venda.tipo_pagamento,
            "pulse_status": venda.status_pulso,
            "is_teste": bool(venda.is_teste),
        }

    transacao = (
        db.query(Transacao)
        .filter(Transacao.maquina_id == machine_id, Transacao.tipo == "IN")
        .order_by(Transacao.data_hora.desc())
        .first()
    )
    if not transacao:
        return None
    metodo = transacao.metodo.value if hasattr(transacao.metodo, "value") else str(transacao.metodo)
    return {
        "data": transacao.data_hora,
        "valor": float(transacao.valor or 0),
        "origem": metodo.lower(),
        "provider": metodo.lower(),
        "payment_type": metodo,
        "pulse_status": "fisico",
        "is_teste": False,
    }


def _latest_pulse(db: Session, machine_id: str):
    historico = (
        db.query(HistoricoOperacao)
        .filter(
            HistoricoOperacao.maquina_id == machine_id,
            HistoricoOperacao.pulse_status.isnot(None),
        )
        .order_by(HistoricoOperacao.created_at.desc())
        .first()
    )
    if not historico:
        return None
    return {
        "data": historico.created_at,
        "status": historico.pulse_status,
        "categoria": historico.categoria,
        "descricao": historico.descricao,
        "command_id": historico.command_id,
    }


def _serialize_health_machine(db: Session, maquina: Maquina, now: datetime):
    status_online = bool(maquina.ultimo_sinal and (now - maquina.ultimo_sinal) < ONLINE_SIGNAL_WINDOW)
    wifi_status = _wifi_health(maquina.wifi_quality)
    firmware_update_status = maquina.firmware_update_status or ""
    firmware_alert = (
        firmware_update_status in OTA_ACTIVE_STATUSES
        or firmware_update_status == "failed"
        or bool(maquina.firmware_target_version and maquina.firmware_target_version != maquina.firmware_version)
    )
    ultimo_pagamento = _latest_payment(db, maquina.id_hardware)
    ultimo_pulso = _latest_pulse(db, maquina.id_hardware)
    pulse_status = str((ultimo_pulso or {}).get("status") or "").lower()
    pulse_alert = pulse_status.startswith("falha") or pulse_status in PULSE_ABSENT_STATUSES
    health_status = _machine_health_status(status_online, wifi_status, firmware_alert, pulse_alert)

    return {
        "id_hardware": maquina.id_hardware,
        "cliente_id": maquina.cliente_id,
        "cliente_nome": maquina.dono.nome_empresa if getattr(maquina, "dono", None) else None,
        "nome": maquina.nome_local,
        "localizacao": maquina.localizacao,
        "health_status": health_status,
        "status_online": status_online,
        "mqtt_status": "conectado" if status_online else "sem_sinal",
        "ultimo_sinal": maquina.ultimo_sinal,
        "wifi_quality": maquina.wifi_quality,
        "wifi_rssi": maquina.wifi_rssi,
        "wifi_status": wifi_status,
        "firmware_version": maquina.firmware_version,
        "firmware_target_version": maquina.firmware_target_version,
        "firmware_update_status": firmware_update_status,
        "firmware_alert": firmware_alert,
        "ultimo_pagamento": ultimo_pagamento,
        "ultimo_pulso": ultimo_pulso,
        "pulse_alert": pulse_alert,
    }


def _matches_health_filters(item: dict, status: str, wifi: str, firmware: str, pulso: str, busca: str) -> bool:
    if status != "todos" and item["health_status"] != status:
        return False
    if wifi != "todos" and item["wifi_status"] != wifi:
        return False
    if firmware == "pendente" and not item["firmware_alert"]:
        return False
    if firmware == "ok" and item["firmware_alert"]:
        return False
    if pulso == "ausente" and not item["pulse_alert"]:
        return False
    if pulso == "confirmado" and item["pulse_alert"]:
        return False
    if busca:
        haystack = " ".join(
            str(value or "")
            for value in [
                item.get("id_hardware"),
                item.get("nome"),
                item.get("localizacao"),
                item.get("cliente_nome"),
                item.get("firmware_version"),
            ]
        ).lower()
        if busca not in haystack:
            return False
    return True


@router.get("/maquinas", response_model=List[MaquinaOut])
def listar_maquinas(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    periodo: str = "mes",
    data_inicio: str = None,
    data_fim: str = None,
    cliente_id: int = None,
    id_hardware: str = None,
):
    _, role, user_cliente_id = user
    maquinas_query = _maquina_query_por_usuario(db, role, user_cliente_id)
    if role == "admin" and cliente_id is not None:
        maquinas_query = maquinas_query.filter(Maquina.cliente_id == cliente_id)
    if id_hardware:
        maquinas_query = maquinas_query.filter(Maquina.id_hardware == id_hardware)

    maquinas = maquinas_query.order_by(Maquina.nome_local.asc(), Maquina.id_hardware.asc()).all()
    return [
        serialize_machine_summary(
            db,
            maquina,
            periodo=periodo,
            data_inicio=data_inicio,
            data_fim=data_fim,
        )
        for maquina in maquinas
    ]


@router.get("/maquinas/saude")
def listar_saude_maquinas(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    cliente_id: int = None,
    status: str = "todos",
    wifi: str = "todos",
    firmware: str = "todos",
    pulso: str = "todos",
    busca: str = "",
):
    _, role, user_cliente_id = user
    query = _maquina_query_por_usuario(db, role, user_cliente_id)
    if role == "admin" and cliente_id is not None:
        query = query.filter(Maquina.cliente_id == cliente_id)

    now = datetime.utcnow()
    normalized_filters = {
        "status": (status or "todos").strip().lower(),
        "wifi": (wifi or "todos").strip().lower(),
        "firmware": (firmware or "todos").strip().lower(),
        "pulso": (pulso or "todos").strip().lower(),
        "busca": (busca or "").strip().lower(),
    }
    maquinas = [
        _serialize_health_machine(db, maquina, now)
        for maquina in query.order_by(Maquina.nome_local.asc(), Maquina.id_hardware.asc()).all()
    ]
    filtered = [
        item
        for item in maquinas
        if _matches_health_filters(item, **normalized_filters)
    ]
    resumo = {
        "total": len(maquinas),
        "online": sum(1 for item in maquinas if item["health_status"] == "online"),
        "atencao": sum(1 for item in maquinas if item["health_status"] == "atencao"),
        "offline": sum(1 for item in maquinas if item["health_status"] == "offline"),
        "wifi_ruim": sum(1 for item in maquinas if item["wifi_status"] == "ruim"),
        "pulso_ausente": sum(1 for item in maquinas if item["pulse_alert"]),
        "firmware_pendente": sum(1 for item in maquinas if item["firmware_alert"]),
        "filtradas": len(filtered),
    }
    return {"resumo": resumo, "maquinas": filtered}


@router.post("/maquinas", response_model=MaquinaOut)
def criar_maquina(
    maquina: MaquinaCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode criar maquinas")
    machine_id = maquina.id_hardware or _generate_machine_id(db)
    if db.query(Maquina).filter(Maquina.id_hardware == machine_id).first():
        raise HTTPException(status_code=400, detail="Maquina ja cadastrada")
    cliente = db.query(Cliente).filter(Cliente.id == maquina.cliente_id).first()
    if not cliente:
        raise HTTPException(status_code=422, detail="Escolha um usuario/cliente valido para criar a maquina")
    banco_pagamento = (maquina.banco_pagamento or "mercado_pago").strip().lower()
    bancos_validos = {"mercado_pago", "pagbank", "s6pay"}
    if banco_pagamento not in bancos_validos:
        raise HTTPException(status_code=422, detail="Banco de pagamento invalido")
    banco_habilitado = {
        "mercado_pago": bool(cliente.cliente_mercado_pago or cliente.mp_access_token),
        "pagbank": bool(cliente.cliente_pagbank),
        "s6pay": bool(cliente.cliente_s6pay),
    }[banco_pagamento]
    if not banco_habilitado:
        raise HTTPException(status_code=422, detail="O banco escolhido nao esta habilitado para este cliente")
    if banco_pagamento != "mercado_pago":
        raise HTTPException(status_code=501, detail="Integracao deste banco ainda nao foi implementada")
    if not cliente.mp_access_token:
        raise HTTPException(
            status_code=422,
            detail="O usuario escolhido ainda nao tem MP_ACCESS_TOKEN cadastrado",
        )

    db_maquina = Maquina(
        id_hardware=machine_id,
        cliente_id=maquina.cliente_id,
        banco_pagamento=banco_pagamento,
        nome_local=maquina.nome,
        localizacao=maquina.localizacao,
        ultimo_sinal=None,
    )
    pos_data = create_pos_for_machine(cliente, db_maquina)
    db_maquina.mp_store_id = pos_data["mp_store_id"]
    db_maquina.mp_store_external_id = pos_data["mp_store_external_id"]
    db_maquina.mp_pos_id = pos_data["mp_pos_id"]
    db_maquina.mp_pos_external_id = pos_data["mp_pos_external_id"]
    db_maquina.mp_qr_image = pos_data["mp_qr_image"]
    db.add(db_maquina)
    registrar_auditoria(
        db,
        user,
        acao="MAQUINA_CRIADA",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=(
            f"Maquina criada cliente_id={maquina.cliente_id} nome={maquina.nome} "
            f"localizacao={maquina.localizacao} banco={banco_pagamento} mp_pos_id={db_maquina.mp_pos_id}"
        ),
    )
    db.commit()
    db.refresh(db_maquina)

    return {
        **serialize_machine_summary(db, db_maquina, periodo="mes"),
    }


@router.put("/maquinas/{machine_id}", response_model=MaquinaOut)
def atualizar_maquina(
    machine_id: str,
    maquina: MaquinaUpdate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode editar maquinas")

    db_maquina = db.query(Maquina).filter(Maquina.id_hardware == machine_id).first()
    if not db_maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")

    nome_anterior = db_maquina.nome_local
    localizacao_anterior = db_maquina.localizacao
    cliente_anterior = db_maquina.cliente_id
    banco_anterior = db_maquina.banco_pagamento
    db_maquina.nome_local = maquina.nome
    db_maquina.localizacao = maquina.localizacao
    db_maquina.cliente_id = maquina.cliente_id
    if maquina.banco_pagamento:
        db_maquina.banco_pagamento = maquina.banco_pagamento
    registrar_auditoria(
        db,
        user,
        acao="MAQUINA_ATUALIZADA",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=(
            f"Maquina atualizada nome={nome_anterior}->{maquina.nome} "
            f"localizacao={localizacao_anterior}->{maquina.localizacao} "
            f"cliente_id={cliente_anterior}->{maquina.cliente_id} "
            f"banco={banco_anterior}->{db_maquina.banco_pagamento}"
        ),
    )
    db.commit()
    db.refresh(db_maquina)

    return serialize_machine_summary(db, db_maquina, periodo="mes")


@router.get("/maquinas/novo-id")
def gerar_novo_id_maquina(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode gerar ids de maquinas")
    return {"id_hardware": _generate_machine_id(db)}


@router.delete("/maquinas/{machine_id}")
def deletar_maquina(
    machine_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode excluir maquinas")

    db_maquina = db.query(Maquina).filter(Maquina.id_hardware == machine_id).first()
    if not db_maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")

    produtos_removidos = db.query(Produto).filter(Produto.maquina_id == machine_id).count()
    transacoes_removidas = db.query(Transacao).filter(Transacao.maquina_id == machine_id).count()
    vendas_removidas = db.query(VendaPagamento).filter(VendaPagamento.maquina_id == machine_id).count()
    historicos_removidos = db.query(HistoricoOperacao).filter(HistoricoOperacao.maquina_id == machine_id).count()
    fechamentos_removidos = db.query(FechamentoMaquina).filter(FechamentoMaquina.maquina_id == machine_id).count()
    auditorias_removidas = db.query(AuditoriaOperacao).filter(AuditoriaOperacao.maquina_id == machine_id).count()
    escutas_removidas = db.query(EscutaTerminal).filter(EscutaTerminal.maquina_id == machine_id).count()
    registrar_auditoria(
        db,
        user,
        acao="MAQUINA_EXCLUIDA",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=(
            f"Maquina excluida nome={db_maquina.nome_local} cliente_id={db_maquina.cliente_id} "
            f"produtos={produtos_removidos} transacoes={transacoes_removidas} "
            f"vendas={vendas_removidas} escutas_terminal={escutas_removidas} "
            f"historicos={historicos_removidos} fechamentos={fechamentos_removidos} "
            f"auditorias_maquina={auditorias_removidas}"
        ),
    )
    db.query(EscutaTerminal).filter(EscutaTerminal.maquina_id == machine_id).delete(synchronize_session=False)
    db.query(VendaPagamento).filter(VendaPagamento.maquina_id == machine_id).delete(synchronize_session=False)
    db.query(AuditoriaOperacao).filter(AuditoriaOperacao.maquina_id == machine_id).delete(synchronize_session=False)
    db.query(FechamentoMaquina).filter(FechamentoMaquina.maquina_id == machine_id).delete(synchronize_session=False)
    db.query(HistoricoOperacao).filter(HistoricoOperacao.maquina_id == machine_id).delete(synchronize_session=False)
    db.query(Transacao).filter(Transacao.maquina_id == machine_id).delete(synchronize_session=False)
    db.query(Produto).filter(Produto.maquina_id == machine_id).delete(synchronize_session=False)
    db.delete(db_maquina)
    db.commit()
    return {"ok": True}

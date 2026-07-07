from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

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
from app.services.maquinas_relatorio import (
    compute_active_alerts,
    compute_all_machines_health,
    serialize_machine_summary,
    serialize_machines_summary_batch,
)
from app.services.mercado_pago import create_pos_for_machine

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _maquina_query_por_usuario(db: Session, role: str, cliente_id):
    # joinedload evita 1 query extra por maquina so para ler o nome do cliente
    # (maquina.dono) quando a listagem tem varias maquinas de clientes diferentes.
    query = db.query(Maquina).options(joinedload(Maquina.dono))
    if role == "admin":
        return query
    return query.filter(Maquina.cliente_id == cliente_id)


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


def _matches_alert_filters(alert: dict, tipo: str, severidade: str, busca: str) -> bool:
    if tipo != "todos" and alert["tipo"] != tipo:
        return False
    if severidade != "todos" and alert["severidade"] != severidade:
        return False
    if busca:
        machine = alert.get("maquina") or {}
        haystack = " ".join(
            str(value or "")
            for value in [
                alert.get("titulo"),
                alert.get("mensagem"),
                machine.get("id_hardware"),
                machine.get("nome"),
                machine.get("cliente_nome"),
                machine.get("localizacao"),
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
    return serialize_machines_summary_batch(
        db,
        maquinas,
        periodo=periodo,
        data_inicio=data_inicio,
        data_fim=data_fim,
    )


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
    maquinas_list = query.order_by(Maquina.nome_local.asc(), Maquina.id_hardware.asc()).all()
    maquinas = compute_all_machines_health(db, maquinas_list, now)
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


@router.get("/maquinas/alertas")
def listar_alertas_maquinas(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    cliente_id: int = None,
    tipo: str = "todos",
    severidade: str = "todos",
    busca: str = "",
):
    _, role, user_cliente_id = user
    query = _maquina_query_por_usuario(db, role, user_cliente_id)
    if role == "admin" and cliente_id is not None:
        query = query.filter(Maquina.cliente_id == cliente_id)

    now = datetime.utcnow()
    normalized_tipo = (tipo or "todos").strip().lower()
    normalized_severidade = (severidade or "todos").strip().lower()
    normalized_busca = (busca or "").strip().lower()
    maquinas_list = query.order_by(Maquina.nome_local.asc(), Maquina.id_hardware.asc()).all()
    alerts = compute_active_alerts(db, maquinas_list, now)
    filtered = [
        alert
        for alert in alerts
        if _matches_alert_filters(alert, normalized_tipo, normalized_severidade, normalized_busca)
    ]
    resumo = {
        "total": len(alerts),
        "criticos": sum(1 for item in alerts if item["severidade"] == "critico"),
        "avisos": sum(1 for item in alerts if item["severidade"] == "aviso"),
        "infos": sum(1 for item in alerts if item["severidade"] == "info"),
        "offline": sum(1 for item in alerts if item["tipo"] == "offline"),
        "wifi_ruim": sum(1 for item in alerts if item["tipo"] == "wifi_ruim"),
        "pulso_ausente": sum(1 for item in alerts if item["tipo"] == "pulso_ausente"),
        "firmware": sum(1 for item in alerts if item["tipo"] == "firmware"),
        "ruido_contador": sum(1 for item in alerts if item["tipo"] == "ruido_contador"),
        "filtrados": len(filtered),
    }
    return {"resumo": resumo, "alertas": filtered}


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

from datetime import datetime, timedelta
import re
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import AuditoriaOperacao, FirmwareVersion, HistoricoOperacao, Maquina, VendaPagamento
from app.services.auditoria import registrar_auditoria
from app.services.mercado_pago import mp_request
from app.services.mqtt_commands import publish_machine_credit, publish_machine_ping, publish_machine_update
from app.services.pagamentos_helpers import extract_provider_payment_id, should_allow_refund
from app.services.command_queue import get_command_status
from app.services.pulse_tracking import update_pulse_status

router = APIRouter()

FIRMWARE_UPDATE_IN_FLIGHT_STATUSES = {"sent", "downloading", "restarting"}
FIRMWARE_UPDATE_LOCK_TIMEOUT = timedelta(minutes=10)

# Mesma tabela de codigos de desconexao Wi-Fi (ESP-IDF wifi_err_reason_t)
# usada no frontend (formatWifiDisconnectReason em SaudeMaquinas.jsx) -
# mantida tambem aqui pra "Historico de quedas" ja devolver o motivo
# traduzido pronto pra exibir.
WIFI_DISCONNECT_REASON_LABELS = {
    2: "Autenticacao expirou",
    3: "Desconexao pelo cliente",
    4: "Associacao expirou",
    6: "Nao autenticado",
    8: "Desconectado (AP saiu)",
    15: "Timeout no handshake (senha errada?)",
    36: "Roteador encerrou a conexao (nao e sinal fraco)",
    200: "Sinal perdido (beacon timeout)",
    201: "Rede nao encontrada",
    202: "Falha de autenticacao",
    203: "Falha de associacao",
    204: "Timeout de handshake",
    205: "Falha ao conectar",
    206: "AP reiniciou (TSF reset)",
    207: "Roaming",
}
FORCED_RESTART_REASON_LABELS = {
    "wifi_offline_5min": "Wi-Fi preso (radio nao voltava mesmo reciclando)",
    "mqtt_offline_5min": "MQTT preso (Wi-Fi conectado mas sem falar com o broker)",
}
_WIFI_DISC_REASON_RE = re.compile(r"wifi_disc_reason=(-?\d+)")
_WIFI_DISC_COUNT_RE = re.compile(r"wifi_disc_count=(-?\d+)")
_FORCED_RESTART_MOTIVO_RE = re.compile(r"reiniciou sozinha apos ficar presa \(motivo: ([^)]+)\)")


def _translate_wifi_disconnect_reason(code: int) -> str:
    label = WIFI_DISCONNECT_REASON_LABELS.get(code)
    return f"{label} (codigo {code})" if label else f"Codigo {code}"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_maquina_visivel(db: Session, machine_id: str, role: str, cliente_id):
    query = db.query(Maquina)
    if role != "admin":
        query = query.filter(Maquina.cliente_id == cliente_id)
    maquina = query.filter(Maquina.id_hardware == machine_id).first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")
    return maquina


def _get_user_email(user) -> str:
    token_data, _, _ = user
    return token_data.email


@router.post("/maquinas/{machine_id}/credito-teste")
def enviar_credito_teste(
    machine_id: str,
    payload: dict,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    _get_maquina_visivel(db, machine_id, role, cliente_id)

    try:
        valor = round(float(payload.get("valor")), 2)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Informe um valor valido") from None
    if valor <= 0:
        raise HTTPException(status_code=422, detail="O valor deve ser maior que zero")
    if valor > 10000:
        raise HTTPException(status_code=422, detail="O valor maximo para teste e R$ 10.000,00")

    command_id = str(uuid4())
    descricao = f"Pagamento de teste enviado pelo painel no valor de R$ {valor:.2f}"

    db.add(
        HistoricoOperacao(
            maquina_id=machine_id,
            categoria="TESTE",
            descricao=descricao,
            valor=valor,
            command_id=command_id,
            pulse_status="pendente",
            created_at=datetime.utcnow(),
        )
    )
    db.add(
        AuditoriaOperacao(
            maquina_id=machine_id,
            acao="TESTE_CREDITO",
            descricao=descricao,
            executado_por_email=_get_user_email(user),
            created_at=datetime.utcnow(),
        )
    )
    registrar_auditoria(
        db,
        user,
        acao="TESTE_CREDITO",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=f"{descricao} command_id={command_id}",
    )
    db.commit()

    try:
        update_pulse_status(command_id, "comando_enviado")
        mqtt_payload = publish_machine_credit(
            machine_id,
            action="paid",
            command_id=command_id,
            amount=valor,
        )
    except Exception as exc:
        update_pulse_status(command_id, "falha_publicacao")
        raise HTTPException(status_code=502, detail="Falha ao enviar comando MQTT para a maquina") from exc

    # Nao fica mais travado esperando a maquina confirmar (cada pulso fisico
    # leva segundos, e um teste maior podia estourar o tempo de resposta do
    # HTTP). O comando pode ter ido direto (status "enviado") ou ter entrado
    # na fila se a maquina ja estava processando outro pagamento (status
    # "na_fila") - o painel acompanha o resultado consultando
    # GET /comandos-maquinas/{command_id}.
    command_status = get_command_status(command_id) or "pendente"

    return {
        "ok": True,
        "machine_id": machine_id,
        "topic": f"/TEF/{machine_id}/cmd",
        "payload": mqtt_payload,
        "valor": valor,
        "command_id": command_id,
        "command_status": command_status,
    }


@router.post("/maquinas/{machine_id}/filtro-saida-pos-credito")
def alternar_filtro_saida_pos_credito(
    machine_id: str,
    payload: dict,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Liga/desliga, so para esta maquina, o filtro que ignora um
    'PELUCIA ENTREGUE (OUT)' quando ele chega poucos segundos depois de uma
    liberacao de credito - usado em maquinas com interferencia eletrica do
    driver de credito no sensor OUT (falso positivo de entrega)."""
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode alterar esse filtro")

    maquina = db.query(Maquina).filter(Maquina.id_hardware == machine_id).first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")

    ativo = bool(payload.get("ativo"))
    anterior = bool(maquina.ignorar_saida_pos_credito)
    maquina.ignorar_saida_pos_credito = ativo
    registrar_auditoria(
        db,
        user,
        acao="FILTRO_SAIDA_POS_CREDITO",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=f"Filtro de saida pos-credito alterado de {anterior} para {ativo}",
    )
    db.commit()

    return {"ok": True, "machine_id": machine_id, "ignorar_saida_pos_credito": ativo}


@router.get("/maquinas/{machine_id}/eventos-dispositivo")
def listar_eventos_dispositivo(
    machine_id: str,
    limit: int = 200,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Log tecnico bruto que a placa manda por MQTT (config de pulso/moeda,
    velocidade/largura do pulso do noteiro, wifi, reinicios, etc.) - a mesma
    informacao que hoje so da pra ver nos logs do Render, direto no painel
    para configurar uma maquina nova sem precisar abrir outra aba."""
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode ver o diagnostico da placa")

    maquina = db.query(Maquina).filter(Maquina.id_hardware == machine_id).first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")

    eventos = (
        db.query(HistoricoOperacao)
        .filter(HistoricoOperacao.maquina_id == machine_id, HistoricoOperacao.categoria == "DISPOSITIVO")
        .order_by(HistoricoOperacao.created_at.desc())
        .limit(min(max(limit, 1), 200))
        .all()
    )
    return {
        "machine_id": machine_id,
        "eventos": [
            {
                "id": item.id,
                "created_at": item.created_at,
                "descricao": item.descricao,
                "pulse_status": item.pulse_status,
                "command_id": item.command_id,
            }
            for item in eventos
        ],
    }


@router.get("/maquinas/quedas")
def listar_quedas(
    maquina_id: str | None = None,
    cliente_id: int | None = None,
    data_inicio: str | None = None,
    data_fim: str | None = None,
    limit: int = 300,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Historico consolidado de quedas (conexao caiu de forma suja ou a placa
    se reiniciou sozinha por ter travado) de todas as maquinas visiveis ao
    usuario, com filtro por maquina e por periodo - pra nao precisar abrir um
    diagnostico por maquina pra montar esse quadro na mao. Visivel pra
    qualquer papel (cada um ve so as maquinas que ja enxerga hoje); o filtro
    por cliente (cliente_id) so faz efeito pra admin, que enxerga todos."""
    _, role, user_cliente_id = user

    maquinas_query = db.query(Maquina)
    if role == "admin":
        if cliente_id is not None:
            maquinas_query = maquinas_query.filter(Maquina.cliente_id == cliente_id)
    else:
        maquinas_query = maquinas_query.filter(Maquina.cliente_id == user_cliente_id)
    if maquina_id:
        maquinas_query = maquinas_query.filter(Maquina.id_hardware == maquina_id)
    maquinas = {m.id_hardware: m for m in maquinas_query.all()}
    if not maquinas:
        return {"quedas": [], "total": 0}

    query = db.query(HistoricoOperacao).filter(
        HistoricoOperacao.maquina_id.in_(maquinas.keys()),
        HistoricoOperacao.categoria == "DISPOSITIVO",
        or_(
            HistoricoOperacao.descricao.ilike("Maquina caiu%"),
            HistoricoOperacao.descricao.ilike("Maquina se reiniciou sozinha%"),
        ),
    )
    if data_inicio:
        try:
            query = query.filter(HistoricoOperacao.created_at >= datetime.fromisoformat(data_inicio))
        except ValueError:
            raise HTTPException(status_code=422, detail="data_inicio invalida") from None
    if data_fim:
        try:
            query = query.filter(HistoricoOperacao.created_at <= datetime.fromisoformat(data_fim))
        except ValueError:
            raise HTTPException(status_code=422, detail="data_fim invalida") from None

    total = query.count()
    linhas = (
        query.order_by(HistoricoOperacao.created_at.desc())
        .limit(min(max(limit, 1), 500))
        .all()
    )

    resultado = []
    for linha in linhas:
        maquina = maquinas.get(linha.maquina_id)
        is_reinicio = linha.descricao.startswith("Maquina se reiniciou sozinha")
        motivo_tecnico = None
        wifi_reason_code = None
        wifi_disc_count = None

        if is_reinicio:
            tipo = "reinicio_forcado"
            match = _FORCED_RESTART_MOTIVO_RE.search(linha.descricao)
            motivo_tecnico = match.group(1) if match else None
            motivo = FORCED_RESTART_REASON_LABELS.get(
                motivo_tecnico, motivo_tecnico or "Motivo desconhecido"
            )
        else:
            tipo = "queda_conexao"
            motivo = "Queda de conexao (energia, crash ou rede caiu sem aviso limpo)"

        # A propria linha da queda nao carrega o motivo tecnico do Wi-Fi (o
        # last will e' so "STATUS|OFFLINE") - quem carrega e' o proximo
        # evento de telemetria da mesma maquina, que reporta o ultimo motivo
        # de desconexao conhecido pela placa assim que ela volta a falar.
        proximo = (
            db.query(HistoricoOperacao)
            .filter(
                HistoricoOperacao.maquina_id == linha.maquina_id,
                HistoricoOperacao.categoria == "DISPOSITIVO",
                HistoricoOperacao.created_at > linha.created_at,
            )
            .order_by(HistoricoOperacao.created_at.asc())
            .first()
        )
        reconectou_em = None
        duracao_offline_segundos = None
        if proximo:
            reconectou_em = proximo.created_at
            duracao_offline_segundos = (proximo.created_at - linha.created_at).total_seconds()
            if tipo == "queda_conexao":
                reason_match = _WIFI_DISC_REASON_RE.search(proximo.descricao or "")
                if reason_match:
                    wifi_reason_code = int(reason_match.group(1))
                    motivo = f"{motivo} - {_translate_wifi_disconnect_reason(wifi_reason_code)}"
            count_match = _WIFI_DISC_COUNT_RE.search(proximo.descricao or "")
            if count_match:
                wifi_disc_count = int(count_match.group(1))

        resultado.append(
            {
                "id": linha.id,
                "maquina_id": linha.maquina_id,
                "maquina_nome": maquina.nome_local if maquina else linha.maquina_id,
                "created_at": linha.created_at,
                "tipo": tipo,
                "motivo": motivo,
                "motivo_tecnico": motivo_tecnico,
                "wifi_disconnect_reason_code": wifi_reason_code,
                "wifi_disconnect_count": wifi_disc_count,
                "reconectou_em": reconectou_em,
                "duracao_offline_segundos": duracao_offline_segundos,
            }
        )

    return {"quedas": resultado, "total": total}


@router.get("/maquinas/{machine_id}/caixa")
def consultar_caixa_mercado_pago(
    machine_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Busca no Mercado Pago os dados atuais do caixa (POS) vinculado a essa
    maquina, incluindo o QR code fixo de Pix (imagem, PDF/PNG pra imprimir e
    o codigo copia-e-cola)."""
    _, role, cliente_id = user
    maquina = _get_maquina_visivel(db, machine_id, role, cliente_id)

    cliente = maquina.dono
    access_token = (getattr(cliente, "mp_access_token", None) or "").strip()
    pos_id = (maquina.mp_pos_id or "").strip()
    if not access_token:
        raise HTTPException(status_code=422, detail="Cliente sem Mercado Pago conectado")
    if not pos_id:
        raise HTTPException(status_code=422, detail="Esta maquina ainda nao tem caixa Mercado Pago vinculado")

    pos = mp_request("GET", f"https://api.mercadopago.com/pos/{pos_id}", access_token)
    qr = pos.get("qr") or {}
    return {
        "pos_id": str(pos.get("id") or pos_id),
        "external_id": pos.get("external_id"),
        "name": pos.get("name"),
        "store_id": pos.get("store_id"),
        "fixed_amount": pos.get("fixed_amount"),
        "category": pos.get("category"),
        "qr": {
            "status": qr.get("status"),
            "image": qr.get("image"),
            "template_image": qr.get("template_image"),
            "template_document": qr.get("template_document"),
            "qr_code": qr.get("qr_code"),
            "date_created": qr.get("date_created"),
            "date_last_updated": qr.get("date_last_updated"),
        },
    }


@router.post("/maquinas/{machine_id}/verificar-online")
def verificar_maquina_online(
    machine_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    maquina = _get_maquina_visivel(db, machine_id, role, cliente_id)
    command_id = str(uuid4())

    try:
        publish_machine_ping(machine_id, command_id)
    except Exception as exc:
        maquina.ultimo_sinal = None
        db.commit()
        raise HTTPException(status_code=502, detail="Falha ao enviar verificacao para a placa") from exc

    deadline = time.monotonic() + 6
    responded = False
    while time.monotonic() < deadline:
        db.expire_all()
        response = (
            db.query(HistoricoOperacao)
            .filter(
                HistoricoOperacao.maquina_id == machine_id,
                HistoricoOperacao.categoria == "DISPOSITIVO",
                HistoricoOperacao.command_id == command_id,
                (
                    HistoricoOperacao.descricao.ilike("%status=PONG%")
                    | HistoricoOperacao.descricao.ilike("%status=CMD_RECEBIDO%")
                ),
            )
            .first()
        )
        if response:
            responded = True
            break
        time.sleep(0.25)

    db.refresh(maquina)
    maquina.ultimo_sinal = datetime.utcnow() if responded else None
    db.commit()

    return {
        "ok": True,
        "machine_id": machine_id,
        "online": responded,
        "command_id": command_id,
        "message": "Placa online" if responded else "Placa nao respondeu e foi marcada como offline",
    }


@router.post("/maquinas/{machine_id}/atualizacao")
def enviar_atualizacao_firmware(
    machine_id: str,
    payload: dict | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode enviar atualizacao de firmware")
    maquina = _get_maquina_visivel(db, machine_id, role, cliente_id)

    if not maquina.ultimo_sinal or datetime.utcnow() - maquina.ultimo_sinal > timedelta(seconds=90):
        raise HTTPException(status_code=409, detail="Maquina offline. Aguarde ela ficar online para atualizar.")

    if maquina.firmware_update_status in FIRMWARE_UPDATE_IN_FLIGHT_STATUSES:
        started_at = maquina.firmware_update_requested_at or maquina.firmware_update_started_at
        if started_at and datetime.utcnow() - started_at < FIRMWARE_UPDATE_LOCK_TIMEOUT:
            raise HTTPException(
                status_code=409,
                detail="Ja existe uma atualizacao de firmware em andamento para esta maquina. Aguarde ela terminar.",
            )
        # Trava presa ha mais tempo que o razoavel: provavelmente a placa nunca respondeu
        # a esse comando. Marca como falha por timeout para liberar um novo envio.
        maquina.firmware_update_status = "failed"
        maquina.firmware_update_error = "timeout_sem_resposta_da_placa"
        maquina.firmware_update_finished_at = datetime.utcnow()

    firmware_record = None
    firmware_version_id = (payload or {}).get("firmware_version_id")
    if firmware_version_id:
        try:
            firmware_version_id = int(firmware_version_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Versao de firmware invalida") from exc
        firmware_record = (
            db.query(FirmwareVersion)
            .filter(FirmwareVersion.id == firmware_version_id, FirmwareVersion.ativo.is_(True))
            .first()
        )
        if not firmware_record:
            raise HTTPException(status_code=404, detail="Versao de firmware nao encontrada ou inativa")

    firmware_url = (
        (firmware_record.url_bin if firmware_record else None)
        or (payload or {}).get("url")
        or settings.OTA_FIRMWARE_URL
        or ""
    ).strip()
    firmware_version = (
        (firmware_record.nome if firmware_record else None)
        or (payload or {}).get("version")
        or ""
    ).strip()
    if not firmware_url:
        raise HTTPException(
            status_code=422,
            detail="Cadastre uma versao de firmware ou configure OTA_FIRMWARE_URL no backend",
        )

    command_id = str(uuid4())
    try:
        mqtt_payload = publish_machine_update(
            machine_id,
            firmware_url,
            firmware_version=firmware_version or None,
            command_id=command_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Falha ao enviar comando MQTT de atualizacao") from exc

    maquina.firmware_last_good_version = maquina.firmware_last_good_version or maquina.firmware_version
    maquina.firmware_target_version = firmware_version or None
    maquina.firmware_update_status = "sent"
    maquina.firmware_update_command_id = command_id
    maquina.firmware_update_url = firmware_url
    maquina.firmware_update_requested_at = datetime.utcnow()
    maquina.firmware_update_started_at = None
    maquina.firmware_update_finished_at = None
    maquina.firmware_update_progress = None
    maquina.firmware_update_error = None

    db.add(
        HistoricoOperacao(
            maquina_id=machine_id,
            categoria="DISPOSITIVO",
            descricao=f"Atualizacao OTA enviada url={firmware_url} version={firmware_version or 'n/a'}",
            valor=None,
            command_id=command_id,
            pulse_status="update_enviado",
            created_at=datetime.utcnow(),
        )
    )

    db.add(
        AuditoriaOperacao(
            maquina_id=machine_id,
            acao="ATUALIZACAO_FIRMWARE",
            descricao=f"Atualizacao OTA enviada command_id={command_id} version={firmware_version or 'n/a'} url={firmware_url}",
            executado_por_email=_get_user_email(user),
            created_at=datetime.utcnow(),
        )
    )
    registrar_auditoria(
        db,
        user,
        acao="ATUALIZACAO_FIRMWARE",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=f"Atualizacao OTA enviada command_id={command_id} version={firmware_version or 'n/a'} url={firmware_url}",
    )
    db.commit()

    return {
        "ok": True,
        "machine_id": machine_id,
        "topic": f"/TEF/{machine_id}/cmd",
        "payload": mqtt_payload,
        "command_id": command_id,
        "firmware_version": firmware_version or None,
        "firmware_url": firmware_url,
    }


@router.post("/maquinas/{machine_id}/observacoes")
def registrar_observacao_maquina(
    machine_id: str,
    payload: dict,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    _get_maquina_visivel(db, machine_id, role, cliente_id)

    descricao = (payload.get("descricao") or "").strip()
    if not descricao:
        raise HTTPException(status_code=400, detail="Descricao da observacao e obrigatoria")

    historico = HistoricoOperacao(
        maquina_id=machine_id,
        categoria="MANUTENCAO",
        descricao=descricao,
        valor=None,
        created_at=datetime.utcnow(),
    )
    db.add(historico)
    db.add(
        AuditoriaOperacao(
            maquina_id=machine_id,
            acao="OBSERVACAO_REGISTRADA",
            descricao=descricao,
            executado_por_email=_get_user_email(user),
            created_at=datetime.utcnow(),
        )
    )
    registrar_auditoria(
        db,
        user,
        acao="OBSERVACAO_REGISTRADA",
        entidade_tipo="maquina",
        entidade_id=machine_id,
        descricao=f"Observacao registrada: {descricao}",
    )
    db.commit()
    db.refresh(historico)
    return {
        "id": historico.id,
        "maquina_id": historico.maquina_id,
        "categoria": historico.categoria,
        "descricao": historico.descricao,
        "valor": historico.valor,
        "created_at": historico.created_at,
    }


@router.post("/maquinas/{machine_id}/pagamentos/{historico_id}/extorno")
def estornar_pagamento_maquina(
    machine_id: str,
    historico_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _, role, cliente_id = user
    maquina = _get_maquina_visivel(db, machine_id, role, cliente_id)
    historico = (
        db.query(HistoricoOperacao)
        .filter(
            HistoricoOperacao.id == historico_id,
            HistoricoOperacao.maquina_id == machine_id,
            HistoricoOperacao.categoria == "PAGAMENTO",
        )
        .first()
    )
    if not historico:
        raise HTTPException(status_code=404, detail="Pagamento nao encontrado")
    if historico.refunded_at:
        raise HTTPException(status_code=400, detail="Pagamento ja foi estornado")
    payment_id = extract_provider_payment_id(historico)
    if not should_allow_refund(historico.pulse_status, historico.refunded_at, payment_id, historico.provider):
        raise HTTPException(status_code=422, detail="Extorno permitido apenas para pagamentos com identificador do Mercado Pago e ainda nao estornados")
    if not payment_id:
        raise HTTPException(status_code=422, detail="Pagamento sem payment_id do Mercado Pago para estorno automatico")

    token = (maquina.dono.mp_access_token if getattr(maquina, "dono", None) else "") or ""
    if not token:
        raise HTTPException(status_code=422, detail="Cliente sem token Mercado Pago para estorno")

    mp_request(
        "POST",
        f"https://api.mercadopago.com/v1/payments/{payment_id}/refunds",
        token.strip(),
        body={},
        headers={"X-Idempotency-Key": f"refund-{payment_id}-{historico_id}"},
    )
    refunded_at = datetime.utcnow()
    historico.refunded_at = refunded_at
    venda = db.query(VendaPagamento).filter(VendaPagamento.historico_id == historico.id).first()
    if venda:
        venda.refunded_at = refunded_at
    db.add(
        AuditoriaOperacao(
            maquina_id=machine_id,
            acao="EXTORNO",
            descricao=f"Extorno solicitado para payment_id={payment_id}",
            executado_por_email=_get_user_email(user),
        )
    )
    registrar_auditoria(
        db,
        user,
        acao="EXTORNO",
        entidade_tipo="pagamento",
        entidade_id=historico_id,
        descricao=f"Extorno Mercado Pago solicitado maquina_id={machine_id} payment_id={payment_id}",
    )
    db.commit()
    return {"ok": True, "payment_id": payment_id, "refunded_at": historico.refunded_at}

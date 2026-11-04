"""Redes Wi-Fi reserva de cada maquina (rodizio, firmware 2.9.0+).

Se a rede principal cair, a placa tenta cada rede 3 vezes e passa para a
proxima: principal -> reserva 1 -> ... -> reserva 4 -> principal. A lista fica
gravada na placa numa area propria da memoria (o reset do Wi-Fi pelo botao nao
apaga) e aqui, com a senha criptografada, para o painel mostrar e editar.

Mesmo acesso de wifi.py: admin ve todas, cliente so as proprias maquinas.
"""
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.v1.endpoints.maquinas_operacoes import _firmware_version_tuple
from app.api.v1.endpoints.wifi import _email_usuario, get_db
from app.core.dependencies import get_current_user
from app.core.escopo import obter_maquina_do_usuario
from app.models.models import ComandoMaquina, MaquinaWifiReserva
from app.services.auditoria import registrar_auditoria
from app.services.maquinas_relatorio import ONLINE_SIGNAL_WINDOW
from app.services.mqtt_commands import publish_machine_wifi_reservas
from app.services.wifi_credenciais import criptografar_senha, descriptografar_senha, validar_rede

router = APIRouter()

MIN_FIRMWARE_VERSION_FOR_RESERVAS = (2, 9, 0)
RESERVAS_MAX = 4


def _suporta_reservas(firmware_version: str | None) -> bool:
    versao = _firmware_version_tuple(firmware_version)
    return versao is not None and versao >= MIN_FIRMWARE_VERSION_FOR_RESERVAS


def _situacao_na_placa(db: Session, machine_id: str) -> dict | None:
    comando = (
        db.query(ComandoMaquina)
        .filter(ComandoMaquina.maquina_id == machine_id, ComandoMaquina.tipo == "wifi_reservas")
        .order_by(ComandoMaquina.created_at.desc())
        .first()
    )
    if not comando:
        return None
    if comando.status == "executado":
        situacao = "gravado"
    elif comando.status == "falhou":
        situacao = "falhou"
    else:
        situacao = "enviando"
    return {
        "situacao": situacao,
        "detalhe": comando.detalhe_status or comando.ultimo_erro,
        "enviado_em": comando.created_at,
        "finalizado_em": comando.finished_at,
    }


@router.get("/maquinas/{machine_id}/wifi/reservas")
def listar_reservas(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    maquina = obter_maquina_do_usuario(db, user, machine_id)
    reservas = (
        db.query(MaquinaWifiReserva)
        .filter(MaquinaWifiReserva.maquina_id == machine_id)
        .order_by(MaquinaWifiReserva.ordem)
        .all()
    )
    return {
        "redes": [{"ordem": r.ordem, "ssid": r.ssid, "tem_senha": bool(r.senha_cript)} for r in reservas],
        "max": RESERVAS_MAX,
        "suporta": _suporta_reservas(maquina.firmware_version),
        "versao_minima": ".".join(str(parte) for parte in MIN_FIRMWARE_VERSION_FOR_RESERVAS),
        "na_placa": _situacao_na_placa(db, machine_id),
        "atualizado_por": reservas[0].atualizado_por if reservas else None,
        "atualizado_em": reservas[0].updated_at if reservas else None,
    }


@router.put("/maquinas/{machine_id}/wifi/reservas")
def salvar_reservas(machine_id: str, payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Substitui a lista inteira e manda para a placa. Em cada rede, senha
    ausente (null) mantem a senha ja salva para essa mesma rede."""
    maquina = obter_maquina_do_usuario(db, user, machine_id)
    if not _suporta_reservas(maquina.firmware_version):
        minima = ".".join(str(parte) for parte in MIN_FIRMWARE_VERSION_FOR_RESERVAS)
        raise HTTPException(
            status_code=409,
            detail=(
                f"Esta maquina esta com firmware {maquina.firmware_version or 'desconhecido'}. "
                f"Atualize para a versao {minima} ou mais recente para usar redes reserva."
            ),
        )
    if not maquina.ultimo_sinal or datetime.utcnow() - maquina.ultimo_sinal > ONLINE_SIGNAL_WINDOW:
        raise HTTPException(status_code=409, detail="A maquina precisa estar online para isso")

    redes_in = payload.get("redes")
    if not isinstance(redes_in, list):
        raise HTTPException(status_code=422, detail="Lista de redes invalida")
    if len(redes_in) > RESERVAS_MAX:
        raise HTTPException(status_code=422, detail=f"No maximo {RESERVAS_MAX} redes reserva")

    atuais = {
        r.ssid: r
        for r in db.query(MaquinaWifiReserva).filter(MaquinaWifiReserva.maquina_id == machine_id).all()
    }
    redes: list[tuple[str, str]] = []
    for posicao, item in enumerate(redes_in, start=1):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail="Lista de redes invalida")
        ssid = str(item.get("ssid") or "")
        if item.get("senha") is None:
            anterior = atuais.get(ssid)
            senha = descriptografar_senha(anterior.senha_cript) if anterior else ""
            if senha is None:
                raise HTTPException(
                    status_code=422, detail=f"Rede {posicao}: nao foi possivel ler a senha salva, digite de novo"
                )
        else:
            senha = str(item.get("senha"))
        erro = validar_rede(ssid, senha)
        if erro:
            raise HTTPException(status_code=422, detail=f"Rede {posicao}: {erro}")
        if any(ssid == existente for existente, _ in redes):
            raise HTTPException(status_code=422, detail=f"A rede {ssid!r} aparece mais de uma vez")
        redes.append((ssid, senha))

    agora = datetime.utcnow()
    email = _email_usuario(user)
    db.query(MaquinaWifiReserva).filter(MaquinaWifiReserva.maquina_id == machine_id).delete()
    for ordem, (ssid, senha) in enumerate(redes, start=1):
        db.add(
            MaquinaWifiReserva(
                maquina_id=machine_id,
                ordem=ordem,
                ssid=ssid,
                senha_cript=criptografar_senha(senha) if senha else "",
                atualizado_por=email,
                updated_at=agora,
            )
        )
    nomes = ", ".join(repr(ssid) for ssid, _ in redes) or "nenhuma"
    registrar_auditoria(
        db, user, "wifi_reservas", "maquina", machine_id, f"Definiu as redes reserva da maquina {machine_id}: {nomes}"
    )
    db.commit()

    command_id = str(uuid4())
    try:
        publish_machine_wifi_reservas(machine_id, command_id, redes)
    except Exception:
        raise HTTPException(
            status_code=502, detail="Lista salva no painel, mas nao foi possivel enviar para a maquina"
        ) from None
    return {"command_id": command_id}

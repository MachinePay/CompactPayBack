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

from app.api.v1.endpoints.wifi import _email_usuario, get_db
from app.core.dependencies import get_current_user
from app.core.escopo import obter_maquina_do_usuario
from app.api.v1.endpoints.maquinas_operacoes import _firmware_version_tuple
from app.models.models import Maquina, MaquinaWifiReserva, MaquinaWifiTentativa
from app.services import mqtt_commands
from app.services.maquinas_relatorio import ONLINE_SIGNAL_WINDOW
from app.services.auditoria import registrar_auditoria
from app.services.wifi_credenciais import criptografar_senha, descriptografar_senha, validar_rede
from app.services.wifi_reservas import (
    MIN_FIRMWARE_VERSION_FOR_RESERVAS,
    enviar_reservas_pendentes,
    suporta_reservas,
    ultimo_envio,
)

router = APIRouter()

RESERVAS_MAX = 4
# @wifi_testar (testa sem trocar) chegou depois do rodizio.
MIN_FIRMWARE_VERSION_FOR_TESTE = (2, 9, 2)


def _situacao_na_placa(db: Session, maquina: Maquina) -> dict | None:
    comando = ultimo_envio(db, maquina.id_hardware)
    if maquina.wifi_reservas_pendente and (not comando or comando.status in ("executado", "falhou")):
        # Salva no painel, esperando a maquina ficar online (com firmware 2.9.0+).
        return {"situacao": "pendente", "detalhe": None, "enviado_em": None, "finalizado_em": None}
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
        "suporta": suporta_reservas(maquina.firmware_version),
        "suporta_teste": (_firmware_version_tuple(maquina.firmware_version) or (0, 0, 0)) >= MIN_FIRMWARE_VERSION_FOR_TESTE,
        "versao_minima": ".".join(str(parte) for parte in MIN_FIRMWARE_VERSION_FOR_RESERVAS),
        "na_placa": _situacao_na_placa(db, maquina),
        "atualizado_por": reservas[0].atualizado_por if reservas else None,
        "atualizado_em": reservas[0].updated_at if reservas else None,
    }


@router.put("/maquinas/{machine_id}/wifi/reservas")
def salvar_reservas(machine_id: str, payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Substitui a lista inteira. Em cada rede, senha ausente (null) mantem a
    senha ja salva para essa mesma rede. Salva mesmo com a maquina offline ou
    com firmware antigo: a placa recebe quando estiver online com 2.9.0+."""
    maquina = obter_maquina_do_usuario(db, user, machine_id)

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
    maquina.wifi_reservas_pendente = True
    nomes = ", ".join(repr(ssid) for ssid, _ in redes) or "nenhuma"
    registrar_auditoria(
        db, user, "wifi_reservas", "maquina", machine_id, f"Definiu as redes reserva da maquina {machine_id}: {nomes}"
    )
    db.commit()

    try:
        command_id = enviar_reservas_pendentes(db, maquina, em_segundo_plano=False)
    except Exception:
        # Continua pendente: o worker tenta de novo no proximo sinal da placa.
        command_id = None
    return {"command_id": command_id, "enviado_agora": command_id is not None}


@router.post("/maquinas/{machine_id}/wifi/reservas/testar")
def testar_rede_reserva(machine_id: str, payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Testa uma rede (Wi-Fi + servidor) sem trocar: a placa sempre volta para
    a rede atual. Senha null = a senha ja salva dessa rede reserva. O resultado
    chega em GET /maquinas/{id}/wifi/troca/{cmd} ("testado" ou "falhou")."""
    ssid = str(payload.get("ssid") or "")
    if payload.get("senha") is None:
        salva = (
            db.query(MaquinaWifiReserva)
            .filter(MaquinaWifiReserva.maquina_id == machine_id, MaquinaWifiReserva.ssid == ssid)
            .first()
        )
        senha = descriptografar_senha(salva.senha_cript) if salva else ""
        if senha is None:
            raise HTTPException(status_code=422, detail="Nao foi possivel ler a senha salva, digite de novo")
    else:
        senha = str(payload.get("senha"))
    erro = validar_rede(ssid, senha)
    if erro:
        raise HTTPException(status_code=422, detail=erro)

    maquina = obter_maquina_do_usuario(db, user, machine_id)
    versao = _firmware_version_tuple(maquina.firmware_version)
    if versao is None or versao < MIN_FIRMWARE_VERSION_FOR_TESTE:
        minima = ".".join(str(parte) for parte in MIN_FIRMWARE_VERSION_FOR_TESTE)
        raise HTTPException(
            status_code=409,
            detail=f"Para testar a rede a maquina precisa do firmware {minima} ou mais novo. Da para salvar sem testar.",
        )
    if not maquina.ultimo_sinal or datetime.utcnow() - maquina.ultimo_sinal > ONLINE_SIGNAL_WINDOW:
        raise HTTPException(status_code=409, detail="A maquina precisa estar online para testar a rede")
    em_andamento = (
        db.query(MaquinaWifiTentativa)
        .filter(MaquinaWifiTentativa.maquina_id == machine_id, MaquinaWifiTentativa.resultado == "testando")
        .order_by(MaquinaWifiTentativa.created_at.desc())
        .first()
    )
    if em_andamento and (datetime.utcnow() - em_andamento.created_at).total_seconds() < 180:
        raise HTTPException(status_code=409, detail="Ja existe um teste ou troca de Wi-Fi em andamento nesta maquina")

    command_id = str(uuid4())
    db.add(
        MaquinaWifiTentativa(
            maquina_id=machine_id,
            command_id=command_id,
            ssid=ssid,
            senha_cript=criptografar_senha(senha) if senha else "",
            origem="teste",
            resultado="testando",
            solicitado_por=_email_usuario(user),
            created_at=datetime.utcnow(),
        )
    )
    registrar_auditoria(
        db, user, "testar_wifi", "maquina", machine_id, f"Pediu teste da rede {ssid!r} na maquina {machine_id}"
    )
    db.commit()
    try:
        mqtt_commands.publish_machine_wifi_teste(machine_id, command_id, ssid, senha)
    except Exception:
        tentativa = db.query(MaquinaWifiTentativa).filter(MaquinaWifiTentativa.command_id == command_id).first()
        tentativa.resultado = "falhou"
        tentativa.motivo = "falha_publicacao"
        tentativa.finished_at = datetime.utcnow()
        db.commit()
        raise HTTPException(status_code=502, detail="Nao foi possivel enviar o comando para a maquina") from None
    return {"command_id": command_id}

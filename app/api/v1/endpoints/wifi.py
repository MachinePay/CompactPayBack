"""Wi-Fi das maquinas: troca remota, rede atual e historico de redes/senhas.

Tudo aqui e' so para admin: mostra senhas de Wi-Fi dos locais dos clientes.
Ver senhas e trocar Wi-Fi ficam registrados na auditoria.
"""
import json
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import ComandoMaquina, Maquina, MaquinaWifiCredencial, MaquinaWifiTentativa
from app.services.auditoria import registrar_auditoria
from app.services.maquinas_relatorio import ONLINE_SIGNAL_WINDOW
from app.services.mqtt_commands import (
    publish_machine_wifi,
    publish_machine_wifi_info_request,
    publish_machine_wifi_scan,
)
from app.services.wifi_credenciais import (
    criptografar_senha,
    descriptografar_senha,
    fechar_tentativas_sem_confirmacao,
    motivo_legivel,
    validar_rede,
)
from app.api.v1.endpoints.maquinas_operacoes import _firmware_version_tuple

router = APIRouter()

MIN_FIRMWARE_VERSION_FOR_WIFI = (2, 8, 0)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _exigir_admin(user) -> None:
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode ver ou trocar o Wi-Fi das maquinas")


def _maquina_pronta_para_wifi(db: Session, machine_id: str) -> Maquina:
    maquina = db.query(Maquina).filter(Maquina.id_hardware == machine_id).first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")
    versao = _firmware_version_tuple(maquina.firmware_version)
    if versao is None or versao < MIN_FIRMWARE_VERSION_FOR_WIFI:
        minima = ".".join(str(parte) for parte in MIN_FIRMWARE_VERSION_FOR_WIFI)
        raise HTTPException(
            status_code=409,
            detail=(
                f"Esta maquina esta com firmware {maquina.firmware_version or 'desconhecido'}. "
                f"Atualize para a versao {minima} ou mais recente para usar o Wi-Fi pelo painel."
            ),
        )
    if not maquina.ultimo_sinal or datetime.utcnow() - maquina.ultimo_sinal > ONLINE_SIGNAL_WINDOW:
        raise HTTPException(status_code=409, detail="A maquina precisa estar online para isso")
    return maquina


def _senha_para_tela(senha_cript: str | None) -> str | None:
    senha = descriptografar_senha(senha_cript)
    return "(nao foi possivel ler - chave mudou)" if senha is None else senha


@router.post("/maquinas/{machine_id}/wifi")
def trocar_wifi(machine_id: str, payload: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Pede para a placa trocar de rede. Ela confere se a rede esta no ar,
    testa a conexao (Wi-Fi + servidor) e, se nao der certo, volta sozinha para
    a rede antiga. O resultado chega em GET /maquinas/{id}/wifi/troca/{cmd}."""
    _exigir_admin(user)
    ssid = str(payload.get("ssid") or "")
    senha = str(payload.get("senha") or "")
    erro = validar_rede(ssid, senha)
    if erro:
        raise HTTPException(status_code=422, detail=erro)
    maquina = _maquina_pronta_para_wifi(db, machine_id)
    em_andamento = (
        db.query(MaquinaWifiTentativa)
        .filter(MaquinaWifiTentativa.maquina_id == machine_id, MaquinaWifiTentativa.resultado == "testando")
        .order_by(MaquinaWifiTentativa.created_at.desc())
        .first()
    )
    if em_andamento and (datetime.utcnow() - em_andamento.created_at).total_seconds() < 180:
        raise HTTPException(status_code=409, detail="Ja existe uma troca de Wi-Fi em andamento nesta maquina")

    command_id = str(uuid4())
    db.add(
        MaquinaWifiTentativa(
            maquina_id=maquina.id_hardware,
            command_id=command_id,
            ssid=ssid,
            senha_cript=criptografar_senha(senha) if senha else "",
            resultado="testando",
            solicitado_por=_email_usuario(user),
            created_at=datetime.utcnow(),
        )
    )
    registrar_auditoria(
        db,
        user,
        "trocar_wifi",
        "maquina",
        machine_id,
        f"Pediu troca do Wi-Fi da maquina {machine_id} para a rede {ssid!r}",
    )
    db.commit()
    try:
        publish_machine_wifi(machine_id, command_id, ssid, senha)
    except Exception:
        tentativa = db.query(MaquinaWifiTentativa).filter(MaquinaWifiTentativa.command_id == command_id).first()
        if tentativa:
            tentativa.resultado = "falhou"
            tentativa.motivo = "falha_publicacao"
            tentativa.finished_at = datetime.utcnow()
            db.commit()
        raise HTTPException(status_code=502, detail="Nao foi possivel enviar o comando para a maquina") from None
    return {"command_id": command_id}


@router.get("/maquinas/{machine_id}/wifi/troca/{command_id}")
def status_troca_wifi(machine_id: str, command_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _exigir_admin(user)
    tentativa = (
        db.query(MaquinaWifiTentativa)
        .filter(MaquinaWifiTentativa.maquina_id == machine_id, MaquinaWifiTentativa.command_id == command_id)
        .first()
    )
    if not tentativa:
        raise HTTPException(status_code=404, detail="Troca de Wi-Fi nao encontrada")
    comando = db.query(ComandoMaquina).filter(ComandoMaquina.command_id == command_id).first()
    # Placa nunca respondeu (offline ou firmware sem suporte): o comando falha
    # por falta de ACK e a tentativa fecha como "sem resposta".
    if tentativa.resultado == "testando" and comando and comando.status == "falhou" and not comando.ack_at:
        tentativa.resultado = "falhou"
        tentativa.motivo = "sem_resposta"
        tentativa.finished_at = datetime.utcnow()
        db.commit()
    if tentativa.resultado == "testando":
        fechar_tentativas_sem_confirmacao(db, machine_id)
        db.commit()
        db.refresh(tentativa)
    etapa = tentativa.resultado
    if etapa == "testando":
        etapa = "testando" if comando and comando.ack_at else "enviando"
    return {
        "resultado": tentativa.resultado,
        "etapa": etapa,
        "motivo": tentativa.motivo,
        "motivo_texto": motivo_legivel(tentativa.motivo),
        "ssid": tentativa.ssid,
    }


@router.post("/maquinas/{machine_id}/wifi/solicitar")
def solicitar_wifi_atual(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Pede para a placa reenviar a rede/senha atuais (maquinas que ja
    estavam configuradas antes do firmware 2.8.0 so mandam quando a rede muda)."""
    _exigir_admin(user)
    _maquina_pronta_para_wifi(db, machine_id)
    command_id = str(uuid4())
    publish_machine_wifi_info_request(machine_id, command_id)
    return {"command_id": command_id}


@router.post("/maquinas/{machine_id}/wifi/procurar")
def procurar_redes(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Pede para a placa listar as redes Wi-Fi perto dela (sem desconectar).
    O resultado chega em GET /maquinas/{id}/wifi/redes."""
    _exigir_admin(user)
    _maquina_pronta_para_wifi(db, machine_id)
    command_id = str(uuid4())
    publish_machine_wifi_scan(machine_id, command_id)
    return {"command_id": command_id, "pedido_em": datetime.utcnow()}


@router.get("/maquinas/{machine_id}/wifi/redes")
def redes_proximas(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _exigir_admin(user)
    maquina = db.query(Maquina).filter(Maquina.id_hardware == machine_id).first()
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada")
    dados = json.loads(maquina.wifi_redes_json) if maquina.wifi_redes_json else {}
    return {
        "atualizado_em": maquina.wifi_redes_em,
        "redes": dados.get("redes", []),
        "total": dados.get("total"),
        "erro": dados.get("erro"),
        "rede_atual": maquina.wifi_ssid,
    }


@router.get("/wifi/historico")
def historico_wifi(
    machine_id: str | None = Query(default=None),
    cliente_id: int | None = Query(default=None),
    data_inicio: datetime | None = Query(default=None),
    data_fim: datetime | None = Query(default=None),
    todas: bool = Query(default=False),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Redes/senhas que cada placa ja usou (sem repetir) e as trocas pedidas
    pelo painel, inclusive as que falharam. Exige cliente ou maquina para nao
    puxar o historico de todas as maquinas sem querer - "todas=true" e' a
    escolha explicita de "Todas as maquinas" no painel."""
    _exigir_admin(user)
    if not machine_id and cliente_id is None and not todas:
        raise HTTPException(status_code=422, detail="Escolha um cliente ou uma maquina")

    maquinas_q = db.query(Maquina)
    if machine_id:
        maquinas_q = maquinas_q.filter(Maquina.id_hardware == machine_id)
    if cliente_id is not None:
        maquinas_q = maquinas_q.filter(Maquina.cliente_id == cliente_id)
    maquinas = {m.id_hardware: m for m in maquinas_q.all()}
    ids = list(maquinas)

    fechar_tentativas_sem_confirmacao(db)
    db.commit()

    credenciais_q = db.query(MaquinaWifiCredencial).filter(MaquinaWifiCredencial.maquina_id.in_(ids))
    tentativas_q = db.query(MaquinaWifiTentativa).filter(MaquinaWifiTentativa.maquina_id.in_(ids))
    # Rede entra se esteve em uso dentro do periodo; tentativa, pela data do pedido.
    if data_inicio:
        credenciais_q = credenciais_q.filter(MaquinaWifiCredencial.ultimo_visto_em >= data_inicio)
        tentativas_q = tentativas_q.filter(MaquinaWifiTentativa.created_at >= data_inicio)
    if data_fim:
        credenciais_q = credenciais_q.filter(MaquinaWifiCredencial.primeiro_visto_em <= data_fim)
        tentativas_q = tentativas_q.filter(MaquinaWifiTentativa.created_at <= data_fim)

    redes = []
    for c in credenciais_q.order_by(MaquinaWifiCredencial.ultimo_visto_em.desc()).all():
        maquina = maquinas.get(c.maquina_id)
        redes.append(
            {
                "id": c.id,
                "maquina_id": c.maquina_id,
                "maquina_nome": maquina.nome_local if maquina else None,
                "ssid": c.ssid,
                "senha": _senha_para_tela(c.senha_cript),
                "atual": bool(maquina and maquina.wifi_credencial_hash == c.credencial_hash),
                "primeiro_visto_em": c.primeiro_visto_em,
                "ultimo_visto_em": c.ultimo_visto_em,
            }
        )

    tentativas = []
    for t in tentativas_q.order_by(MaquinaWifiTentativa.created_at.desc()).limit(300).all():
        maquina = maquinas.get(t.maquina_id)
        tentativas.append(
            {
                "id": t.id,
                "maquina_id": t.maquina_id,
                "maquina_nome": maquina.nome_local if maquina else None,
                "ssid": t.ssid,
                "senha": _senha_para_tela(t.senha_cript),
                "resultado": t.resultado,
                "motivo": t.motivo,
                "motivo_texto": motivo_legivel(t.motivo),
                "solicitado_por": t.solicitado_por,
                "created_at": t.created_at,
                "finished_at": t.finished_at,
            }
        )

    registrar_auditoria(
        db,
        user,
        "ver_senhas_wifi",
        "maquina",
        machine_id,
        f"Abriu o historico de Wi-Fi ({'maquina ' + machine_id if machine_id else f'cliente {cliente_id}' if cliente_id is not None else 'todas as maquinas'})",
    )
    db.commit()
    return {"redes": redes, "tentativas": tentativas}


def _email_usuario(user) -> str | None:
    usuario = user[0]
    return getattr(usuario, "email", None) or (usuario if isinstance(usuario, str) else None)

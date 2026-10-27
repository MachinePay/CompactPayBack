"""Aviso no WhatsApp (Evolution API, mesmo modelo do GrupoGK) quando a maquina
fica offline por mais de X minutos (padrao 60). So para os clientes marcados
na tela Admin > WhatsApp.

- Maquina com ponto fechado (app/services/horario_fechado.py) nao avisa, e o
  tempo offline conta a partir da abertura do ponto.
- Um aviso por numero a cada queda (maquina + ultimo_sinal). Se a maquina
  voltar e cair de novo, e' outra queda.
- Opcional: aviso de "voltou" quando a maquina volta a ficar online.
- Se o WhatsApp estiver desconectado o ciclo e' pulado sem marcar nada, e
  tenta de novo no proximo.
"""

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session, joinedload

from app.db.session import SessionLocal
from app.models.models import Cliente, Maquina, Usuario
from app.models.whatsapp import WhatsappCliente, WhatsappConfig, WhatsappEnvio
from app.services.horario_fechado import fim_ultimo_fechamento, maquina_fechada
from app.services.maquinas_relatorio import ONLINE_SIGNAL_WINDOW
from app.services.wifi_credenciais import criptografar_senha, descriptografar_senha

logger = logging.getLogger(__name__)

BRASILIA_TZ = timezone(timedelta(hours=-3))
INTERVALO_WORKER_SEGUNDOS = int(os.getenv("WHATSAPP_ALERT_INTERVAL_SECONDS", "60"))
PAUSA_ENTRE_MENSAGENS_SEGUNDOS = 3
MINUTOS_OFFLINE_MIN = 5
MINUTOS_OFFLINE_MAX = 24 * 60

MENSAGEM_OFFLINE_PADRAO = (
    "⚠️ *CompactPay* | Aviso de máquina\n\n"
    "Olá, {cliente}!\n\n"
    "A máquina *{maquina}* (ID {maquina_id}) está *sem sinal há {tempo_offline}*.\n"
    "Local: {local}\n"
    "Último sinal: {ultimo_sinal}\n\n"
    "Confira se ela está ligada na tomada e se a internet (Wi-Fi) do local está funcionando."
)
MENSAGEM_VOLTOU_PADRAO = (
    "✅ *CompactPay*\n\n"
    "A máquina *{maquina}* (ID {maquina_id}) voltou a ficar online depois de {tempo_offline} sem sinal."
)
VARIAVEIS = ["cliente", "maquina", "maquina_id", "local", "tempo_offline", "ultimo_sinal"]


class WhatsappErro(Exception):
    def __init__(self, mensagem: str, http_status: int | None = None):
        super().__init__(mensagem)
        self.http_status = http_status


# ---------------------------------------------------------------- config


def obter_config(db: Session) -> WhatsappConfig:
    config = db.get(WhatsappConfig, 1)
    if config is None:
        config = WhatsappConfig(id=1, ativo=False, minutos_offline=60, enviar_voltou=False)
        db.add(config)
        db.flush()
    return config


def _mascarar(chave: str | None) -> str | None:
    if not chave:
        return None
    return "••••" if len(chave) <= 8 else f"{chave[:4]}••••{chave[-4:]}"


def config_para_tela(config: WhatsappConfig) -> dict:
    chave = descriptografar_senha(config.api_key_cript) if config.api_key_cript else None
    return {
        "ativo": bool(config.ativo),
        "evolution_url": config.evolution_url,
        "api_key_configurada": bool(chave),
        "api_key_mascarada": _mascarar(chave),
        "instancia": config.instancia,
        "minutos_offline": config.minutos_offline or 60,
        "mensagem_offline": config.mensagem_offline or MENSAGEM_OFFLINE_PADRAO,
        "enviar_voltou": bool(config.enviar_voltou),
        "mensagem_voltou": config.mensagem_voltou or MENSAGEM_VOLTOU_PADRAO,
        "mensagem_offline_padrao": MENSAGEM_OFFLINE_PADRAO,
        "mensagem_voltou_padrao": MENSAGEM_VOLTOU_PADRAO,
        "variaveis": VARIAVEIS,
        "updated_by": config.updated_by,
        "updated_at": config.updated_at,
    }


def atualizar_config(db: Session, config: WhatsappConfig, dados: dict, usuario_email: str | None) -> None:
    """dados so com os campos enviados. Levanta ValueError com mensagem pronta."""
    if "evolution_url" in dados:
        url = str(dados["evolution_url"] or "").strip().rstrip("/")
        if url and not re.match(r"^https?://[^\s/]+", url, re.IGNORECASE):
            raise ValueError("A URL da Evolution API deve comecar com https://")
        config.evolution_url = url or None
    if str(dados.get("api_key") or "").strip():
        config.api_key_cript = criptografar_senha(str(dados["api_key"]).strip())
    if "instancia" in dados:
        instancia = str(dados["instancia"] or "").strip()
        if instancia and not re.match(r"^[\w.-]{1,60}$", instancia):
            raise ValueError("Nome da instancia: use letras, numeros, ponto, hifen ou _.")
        config.instancia = instancia or None
    if dados.get("minutos_offline") is not None:
        minutos = int(dados["minutos_offline"])
        if minutos < MINUTOS_OFFLINE_MIN or minutos > MINUTOS_OFFLINE_MAX:
            raise ValueError(f"O tempo offline deve ficar entre {MINUTOS_OFFLINE_MIN} e {MINUTOS_OFFLINE_MAX} minutos.")
        config.minutos_offline = minutos
    if "mensagem_offline" in dados:
        texto = str(dados["mensagem_offline"] or "").strip()
        config.mensagem_offline = None if not texto or texto == MENSAGEM_OFFLINE_PADRAO else texto
    if "mensagem_voltou" in dados:
        texto = str(dados["mensagem_voltou"] or "").strip()
        config.mensagem_voltou = None if not texto or texto == MENSAGEM_VOLTOU_PADRAO else texto
    if dados.get("enviar_voltou") is not None:
        config.enviar_voltou = bool(dados["enviar_voltou"])
    if dados.get("ativo") is not None:
        ativo = bool(dados["ativo"])
        if ativo and not (config.evolution_url and config.api_key_cript and config.instancia):
            raise ValueError("Preencha URL, chave e instancia da Evolution API antes de ativar.")
        if ativo and not config.ativo:
            config.ativado_em = datetime.utcnow()
        config.ativo = ativo
    config.updated_by = usuario_email
    config.updated_at = datetime.utcnow()


# ---------------------------------------------------------------- Evolution API


def _chamar(config: WhatsappConfig, caminho: str, method: str = "GET", body: dict | None = None):
    chave = descriptografar_senha(config.api_key_cript) if config.api_key_cript else None
    if not config.evolution_url or not chave:
        raise WhatsappErro("Configure a URL e a chave da Evolution API.")
    try:
        resposta = httpx.request(
            method,
            f"{config.evolution_url}{caminho}",
            headers={"apikey": chave, "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise WhatsappErro(f"Nao foi possivel falar com a Evolution API: {exc}")
    try:
        dados = resposta.json() if resposta.content else None
    except ValueError:
        dados = {"message": resposta.text}
    if resposta.status_code >= 400:
        bruto = None
        if isinstance(dados, dict):
            bruto = (dados.get("response") or {}).get("message") if isinstance(dados.get("response"), dict) else None
            bruto = bruto or dados.get("message") or dados.get("error")
        detalhe = "; ".join(map(str, bruto)) if isinstance(bruto, list) else str(bruto or resposta.reason_phrase)
        if resposta.status_code in (401, 403):
            mensagem = "A Evolution API recusou a chave (apikey). Confira a configuracao."
        elif re.search(r"'exists':\s*False|\"exists\":\s*false", detalhe):
            mensagem = "Este numero nao tem WhatsApp."
        else:
            mensagem = f"Evolution API respondeu {resposta.status_code}: {detalhe}"
        raise WhatsappErro(mensagem, resposta.status_code)
    return dados


def estado_conexao(config: WhatsappConfig) -> str:
    dados = _chamar(config, f"/instance/connectionState/{config.instancia}")
    dados = dados or {}
    return (dados.get("instance") or {}).get("state") or dados.get("state") or "unknown"


def conexao_para_tela(config: WhatsappConfig) -> dict:
    if not (config.evolution_url and config.api_key_cript and config.instancia):
        return {"configurado": False, "estado": None}
    try:
        return {"configurado": True, "estado": estado_conexao(config)}
    except WhatsappErro as exc:
        # Instancia ainda nao criada nao e' erro: ela e' criada ao conectar.
        if exc.http_status == 404:
            return {"configurado": True, "estado": "inexistente"}
        return {"configurado": True, "estado": "erro", "erro": str(exc)}


def conectar(config: WhatsappConfig) -> dict:
    """QR code para conectar o numero. Cria a instancia se ainda nao existir."""
    if not (config.evolution_url and config.api_key_cript and config.instancia):
        raise WhatsappErro("Salve URL, chave e instancia da Evolution API antes de conectar.")
    try:
        estado = estado_conexao(config)
    except WhatsappErro as exc:
        if exc.http_status != 404:
            raise
        estado = "inexistente"
    if estado == "open":
        return {"estado": estado, "qr_base64": None, "pairing_code": None}
    if estado == "inexistente":
        dados = _chamar(
            config,
            "/instance/create",
            method="POST",
            body={"instanceName": config.instancia, "integration": "WHATSAPP-BAILEYS", "qrcode": True},
        ) or {}
        dados = dados.get("qrcode") or dados
    else:
        dados = _chamar(config, f"/instance/connect/{config.instancia}") or {}
    base64 = dados.get("base64")
    if base64 and not base64.startswith("data:"):
        base64 = f"data:image/png;base64,{base64}"
    return {"estado": "connecting", "qr_base64": base64, "pairing_code": dados.get("pairingCode")}


def enviar_texto(config: WhatsappConfig, numero: str, texto: str) -> None:
    _chamar(
        config,
        f"/message/sendText/{config.instancia}",
        method="POST",
        body={"number": numero, "text": texto, "delay": 1200},
    )


# ---------------------------------------------------------------- mensagens


def normalizar_telefone(telefone: str | None) -> str | None:
    digitos = re.sub(r"\D", "", str(telefone or "")).lstrip("0")
    if len(digitos) in (10, 11):
        digitos = f"55{digitos}"
    return digitos if re.fullmatch(r"55\d{10,11}", digitos) else None


def telefones_do_cliente(db: Session, cliente: Cliente) -> list[str]:
    """Telefone do perfil de cada usuario do cliente (o mesmo que aparece na
    tela de Usuarios: o do usuario, ou o do cadastro do cliente se vazio)."""
    usuarios = db.query(Usuario.telefone).filter(Usuario.cliente_id == cliente.id).all()
    brutos = [telefone or cliente.telefone for (telefone,) in usuarios] or [cliente.telefone]
    numeros = []
    for bruto in brutos:
        numero = normalizar_telefone(bruto)
        if numero and numero not in numeros:
            numeros.append(numero)
    return numeros


def formatar_duracao(delta: timedelta) -> str:
    minutos = max(0, int(delta.total_seconds() // 60))
    dias, resto = divmod(minutos, 24 * 60)
    horas, minutos = divmod(resto, 60)
    partes = []
    if dias:
        partes.append(f"{dias} dia" + ("s" if dias > 1 else ""))
    if horas:
        partes.append(f"{horas}h")
    if minutos or not partes:
        partes.append(f"{minutos}min")
    return " ".join(partes)


def _hora_brasilia(momento: datetime | None) -> str:
    if not momento:
        return "-"
    return momento.replace(tzinfo=timezone.utc).astimezone(BRASILIA_TZ).strftime("%d/%m %H:%M")


def montar_mensagem(modelo: str, valores: dict) -> str:
    # replace em vez de str.format: chaves soltas no texto do admin nao quebram.
    texto = modelo
    for nome in VARIAVEIS:
        texto = texto.replace("{" + nome + "}", str(valores.get(nome) or "-"))
    return texto


def valores_maquina(maquina: Maquina, cliente: Cliente | None, offline_desde: datetime, agora: datetime) -> dict:
    return {
        "cliente": cliente.nome_empresa if cliente else "",
        "maquina": maquina.nome_local or maquina.id_hardware,
        "maquina_id": maquina.id_hardware,
        "local": maquina.localizacao,
        "tempo_offline": formatar_duracao(agora - offline_desde),
        "ultimo_sinal": _hora_brasilia(maquina.ultimo_sinal),
    }


VALORES_EXEMPLO = {
    "cliente": "Cliente Exemplo",
    "maquina": "Grua Shopping",
    "maquina_id": "1000",
    "local": "Shopping Centro - piso 2",
    "tempo_offline": "1h 5min",
    "ultimo_sinal": "27/10 14:30",
}


# ---------------------------------------------------------------- worker


def _registrar(db: Session, **campos) -> WhatsappEnvio:
    envio = WhatsappEnvio(created_at=datetime.utcnow(), **campos)
    db.add(envio)
    db.commit()
    return envio


def _enviar_e_registrar(db: Session, config: WhatsappConfig, numero: str, texto: str, **campos) -> bool:
    try:
        enviar_texto(config, numero, texto)
    except WhatsappErro as exc:
        logger.warning("[whatsapp] falha ao enviar para %s: %s", numero, exc)
        _registrar(db, telefone=numero, mensagem=texto, status="erro", erro=str(exc)[:500], **campos)
        return False
    _registrar(db, telefone=numero, mensagem=texto, status="enviado", **campos)
    time.sleep(PAUSA_ENTRE_MENSAGENS_SEGUNDOS)
    return True


def verificar_e_enviar(db: Session, agora: datetime | None = None) -> dict:
    """Um ciclo do worker. Devolve contadores (usado nos testes/log)."""
    agora = agora or datetime.utcnow()
    resultado = {"offline": 0, "voltou": 0, "pulado": None}
    config = db.get(WhatsappConfig, 1)
    if not config or not config.ativo:
        resultado["pulado"] = "desativado"
        return resultado

    escolhidos = {
        item.cliente_id: item
        for item in db.query(WhatsappCliente).filter(WhatsappCliente.ativo.is_(True)).all()
    }
    pendentes_voltou = (
        db.query(WhatsappEnvio)
        .filter(
            WhatsappEnvio.tipo == "offline",
            WhatsappEnvio.status == "enviado",
            WhatsappEnvio.resolvido_em.is_(None),
        )
        .all()
    )
    if not escolhidos and not pendentes_voltou:
        resultado["pulado"] = "sem_clientes"
        return resultado

    try:
        estado = estado_conexao(config)
    except WhatsappErro as exc:
        estado = str(exc)
    if estado != "open":
        logger.warning("[whatsapp] envio pausado, conexao: %s", estado)
        resultado["pulado"] = "desconectado"
        return resultado

    limite = timedelta(minutes=config.minutos_offline or 60)

    # 1) Maquinas que voltaram: fecha o aviso e, se configurado, avisa.
    maquinas_voltou = {
        m.id_hardware: m
        for m in db.query(Maquina)
        .options(joinedload(Maquina.dono))
        .filter(Maquina.id_hardware.in_({e.maquina_id for e in pendentes_voltou}))
        .all()
    } if pendentes_voltou else {}
    for envio in pendentes_voltou:
        maquina = maquinas_voltou.get(envio.maquina_id)
        if maquina is None:
            envio.resolvido_em = agora
            continue
        online = bool(maquina.ultimo_sinal and agora - maquina.ultimo_sinal < ONLINE_SIGNAL_WINDOW)
        if not online:
            continue
        envio.resolvido_em = agora
        db.commit()
        if config.enviar_voltou:
            texto = montar_mensagem(
                config.mensagem_voltou or MENSAGEM_VOLTOU_PADRAO,
                valores_maquina(maquina, maquina.dono, envio.offline_desde or agora, agora),
            )
            if _enviar_e_registrar(
                db,
                config,
                envio.telefone,
                texto,
                tipo="voltou",
                cliente_id=envio.cliente_id,
                maquina_id=envio.maquina_id,
                offline_desde=envio.offline_desde,
                resolvido_em=agora,
            ):
                resultado["voltou"] += 1
    db.commit()

    if not escolhidos:
        return resultado

    # 2) Maquinas offline alem do limite, dos clientes escolhidos.
    maquinas = (
        db.query(Maquina)
        .options(joinedload(Maquina.dono))
        .filter(Maquina.cliente_id.in_(list(escolhidos)), Maquina.ultimo_sinal.isnot(None))
        .all()
    )
    for maquina in maquinas:
        online = agora - maquina.ultimo_sinal < ONLINE_SIGNAL_WINDOW
        if online or maquina_fechada(maquina, online, agora):
            continue
        fim_fechamento = fim_ultimo_fechamento(maquina, agora)
        inicio_offline = max(maquina.ultimo_sinal, fim_fechamento) if fim_fechamento else maquina.ultimo_sinal
        if agora - inicio_offline < limite:
            continue
        # Ja estava offline antes de ligar o aviso (geral ou do cliente): nao manda.
        escolha = escolhidos[maquina.cliente_id]
        ativado_em = max(filter(None, [config.ativado_em, escolha.ativado_em]), default=None)
        if ativado_em and inicio_offline + limite < ativado_em:
            continue

        ja_enviados = {
            telefone
            for (telefone,) in db.query(WhatsappEnvio.telefone).filter(
                WhatsappEnvio.tipo == "offline",
                WhatsappEnvio.maquina_id == maquina.id_hardware,
                WhatsappEnvio.offline_desde == maquina.ultimo_sinal,
            )
        }
        texto = montar_mensagem(
            config.mensagem_offline or MENSAGEM_OFFLINE_PADRAO,
            valores_maquina(maquina, maquina.dono, inicio_offline, agora),
        )
        for numero in telefones_do_cliente(db, maquina.dono):
            if numero in ja_enviados:
                continue
            if _enviar_e_registrar(
                db,
                config,
                numero,
                texto,
                tipo="offline",
                cliente_id=maquina.cliente_id,
                maquina_id=maquina.id_hardware,
                offline_desde=maquina.ultimo_sinal,
            ):
                resultado["offline"] += 1
    return resultado


def run_whatsapp_alert_worker() -> None:
    while True:
        db = SessionLocal()
        try:
            resultado = verificar_e_enviar(db)
            if resultado["offline"] or resultado["voltou"]:
                logger.info("[whatsapp] avisos enviados: %s", resultado)
        except Exception:
            logger.exception("[whatsapp] ciclo de avisos falhou")
            db.rollback()
        finally:
            db.close()
        time.sleep(INTERVALO_WORKER_SEGUNDOS)


def start_whatsapp_alert_worker() -> threading.Thread | None:
    if os.getenv("START_WHATSAPP_ALERT_WORKER", "true").lower() != "true":
        logger.info("WhatsApp alert worker desativado por START_WHATSAPP_ALERT_WORKER=false")
        return None
    thread = threading.Thread(target=run_whatsapp_alert_worker, daemon=True)
    thread.start()
    return thread

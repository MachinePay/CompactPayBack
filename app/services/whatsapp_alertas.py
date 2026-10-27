"""Avisos no WhatsApp (Evolution API, mesmo modelo do GrupoGK). Cada cliente
escolhe na tela Admin > WhatsApp o que recebe:

- offline: maquina sem sinal por mais de X minutos (padrao 60). As maquinas do
  mesmo cliente que cairam juntas vao numa mensagem so.
- lembrete: se continuar offline, novo aviso a cada X horas (padrao 24).
- voltou: quando a maquina avisada volta a ficar online.
- resumo diario: faturamento do dia por maquina, no horario escolhido.

Regras:
- Ponto fechado (horario da semana ou fechamento pontual, ver
  app/services/horario_fechado.py) nao avisa nem lembra, e o tempo offline
  conta a partir da abertura do ponto.
- Um aviso por queda (maquina + ultimo_sinal, WhatsappOfflineAviso). Se a
  maquina voltar e cair de novo, e' outra queda.
- So um processo do backend envia por vez (trava em whatsapp_config).
- WhatsApp desconectado: o ciclo e' pulado sem marcar nada e tenta no proximo.
"""

import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import or_, update
from sqlalchemy.orm import Session, joinedload

from app.db.session import SessionLocal
from app.models.models import Cliente, Maquina, Usuario
from app.models.whatsapp import WhatsappCliente, WhatsappConfig, WhatsappEnvio, WhatsappOfflineAviso
from app.services.horario_fechado import fim_ultimo_fechamento, maquina_fechada
from app.services.maquinas_relatorio import (
    ONLINE_SIGNAL_WINDOW,
    compute_financial_summary_by_machine,
    resolve_date_window,
    transacao_summary_by_machine,
)
from app.services.wifi_credenciais import criptografar_senha, descriptografar_senha

logger = logging.getLogger(__name__)

BRASILIA_TZ = timezone(timedelta(hours=-3))
INTERVALO_WORKER_SEGUNDOS = int(os.getenv("WHATSAPP_ALERT_INTERVAL_SECONDS", "60"))
PAUSA_ENTRE_MENSAGENS_SEGUNDOS = 3
MINUTOS_OFFLINE_MIN = 5
MINUTOS_OFFLINE_MAX = 24 * 60
LEMBRETE_HORAS_MAX = 24 * 7
TRAVA_DURACAO = timedelta(minutes=15)
_PROCESSO_ID = uuid.uuid4().hex
_HORA_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

MENSAGEM_OFFLINE_PADRAO = (
    "⚠️ *CompactPay* | Aviso de máquina\n\n"
    "Olá, {cliente}!\n\n"
    "Máquina(s) sem sinal ({quantidade}):\n"
    "{maquinas}\n\n"
    "Confira se estão ligadas na tomada e se a internet (Wi-Fi) do local está funcionando."
)
MENSAGEM_LEMBRETE_PADRAO = (
    "🔔 *CompactPay* | Lembrete\n\n"
    "Olá, {cliente}! Continuam sem sinal ({quantidade}):\n"
    "{maquinas}\n\n"
    "Se já está resolvendo, pode desconsiderar."
)
MENSAGEM_VOLTOU_PADRAO = (
    "✅ *CompactPay*\n\n"
    "Voltaram a ficar online ({quantidade}):\n"
    "{maquinas}"
)
MENSAGEM_RESUMO_PADRAO = (
    "📊 *CompactPay* | Resumo de {data}\n\n"
    "Olá, {cliente}!\n\n"
    "{maquinas}\n\n"
    "*Total do dia: {total}*\n"
    "{vendas} venda(s) · {pelucias} prêmio(s)"
)

# Variaveis de cada mensagem. As de uma maquina so (maquina, maquina_id,
# local, tempo_offline, ultimo_sinal) continuam valendo: com varias maquinas
# viram a lista dos nomes / o maior tempo.
VARIAVEIS = {
    "offline": ["cliente", "quantidade", "maquinas", "maquina", "maquina_id", "local", "tempo_offline", "ultimo_sinal"],
    "lembrete": ["cliente", "quantidade", "maquinas", "maquina", "maquina_id", "local", "tempo_offline", "ultimo_sinal"],
    "voltou": ["cliente", "quantidade", "maquinas", "maquina", "maquina_id", "tempo_offline"],
    "resumo": ["cliente", "data", "maquinas", "total", "vendas", "pelucias"],
}
_TODAS_VARIAVEIS = sorted({nome for nomes in VARIAVEIS.values() for nome in nomes})

PADROES = {
    "offline": MENSAGEM_OFFLINE_PADRAO,
    "lembrete": MENSAGEM_LEMBRETE_PADRAO,
    "voltou": MENSAGEM_VOLTOU_PADRAO,
    "resumo": MENSAGEM_RESUMO_PADRAO,
}
_CAMPO_MENSAGEM = {
    "offline": "mensagem_offline",
    "lembrete": "mensagem_lembrete",
    "voltou": "mensagem_voltou",
    "resumo": "mensagem_resumo",
}


class WhatsappErro(Exception):
    def __init__(self, mensagem: str, http_status: int | None = None):
        super().__init__(mensagem)
        self.http_status = http_status


# ---------------------------------------------------------------- config


def obter_config(db: Session) -> WhatsappConfig:
    config = db.get(WhatsappConfig, 1)
    if config is None:
        config = WhatsappConfig(
            id=1, ativo=False, minutos_offline=60, enviar_voltou=False, lembrete_horas=24, resumo_horario="22:00"
        )
        db.add(config)
        db.flush()
    return config


def modelo_mensagem(config: WhatsappConfig, tipo: str) -> str:
    return getattr(config, _CAMPO_MENSAGEM[tipo]) or PADROES[tipo]


def validar_hora(valor, campo: str) -> str:
    texto = str(valor or "").strip()
    if not _HORA_RE.match(texto):
        raise ValueError(f"{campo}: use o formato HH:MM")
    return texto


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
        "lembrete_horas": config.lembrete_horas if config.lembrete_horas is not None else 24,
        "resumo_horario": config.resumo_horario or "22:00",
        "mensagens": {tipo: modelo_mensagem(config, tipo) for tipo in PADROES},
        "mensagens_padrao": PADROES,
        "variaveis": VARIAVEIS,
        "ultimo_ciclo_em": config.ultimo_ciclo_em,
        "ultimo_ciclo_status": config.ultimo_ciclo_status,
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
    if dados.get("lembrete_horas") is not None:
        horas = int(dados["lembrete_horas"])
        if horas < 0 or horas > LEMBRETE_HORAS_MAX:
            raise ValueError(f"O lembrete deve ficar entre 1 e {LEMBRETE_HORAS_MAX} horas (0 desliga).")
        config.lembrete_horas = horas
    if dados.get("resumo_horario") is not None:
        config.resumo_horario = validar_hora(dados["resumo_horario"], "Horario do resumo")
    for tipo, texto in (dados.get("mensagens") or {}).items():
        if tipo not in PADROES:
            continue
        texto = str(texto or "").strip()
        setattr(config, _CAMPO_MENSAGEM[tipo], None if not texto or texto == PADROES[tipo] else texto)
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


def _local(momento: datetime) -> datetime:
    return momento.replace(tzinfo=timezone.utc).astimezone(BRASILIA_TZ)


def _hora_brasilia(momento: datetime | None) -> str:
    return _local(momento).strftime("%d/%m %H:%M") if momento else "-"


def formatar_moeda(valor: float) -> str:
    texto = f"{float(valor or 0):,.2f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def montar_mensagem(modelo: str, valores: dict) -> str:
    # replace em vez de str.format: chaves soltas no texto do admin nao quebram.
    texto = modelo
    for nome in _TODAS_VARIAVEIS:
        if nome in valores:
            valor = valores[nome]
            texto = texto.replace("{" + nome + "}", "-" if valor is None or valor == "" else str(valor))
    return texto


def valores_grupo(cliente: Cliente | None, itens: list[tuple], agora: datetime, tipo: str) -> dict:
    """itens: [(maquina, inicio_offline)] do mesmo cliente."""
    itens = sorted(itens, key=lambda item: item[1])
    linhas = []
    for maquina, inicio in itens:
        nome = maquina.nome_local or maquina.id_hardware
        tempo = formatar_duracao(agora - inicio)
        if tipo == "voltou":
            linhas.append(f"• *{nome}* (ID {maquina.id_hardware}) – ficou {tempo} sem sinal")
        else:
            local = f" – {maquina.localizacao}" if maquina.localizacao else ""
            linhas.append(f"• *{nome}* (ID {maquina.id_hardware}){local} – sem sinal há {tempo}")
    locais = list(dict.fromkeys(m.localizacao for m, _ in itens if m.localizacao))
    return {
        "cliente": cliente.nome_empresa if cliente else "",
        "quantidade": len(itens),
        "maquinas": "\n".join(linhas),
        "maquina": ", ".join(m.nome_local or m.id_hardware for m, _ in itens),
        "maquina_id": ", ".join(m.id_hardware for m, _ in itens),
        "local": ", ".join(locais),
        "tempo_offline": formatar_duracao(agora - itens[0][1]) if itens else "",
        "ultimo_sinal": _hora_brasilia(itens[0][0].ultimo_sinal) if itens else "",
    }


def valores_resumo(db: Session, cliente: Cliente, maquinas: list[Maquina], agora: datetime) -> dict:
    hoje = _local(agora).date()
    inicio, fim = resolve_date_window(None, hoje.isoformat(), hoje.isoformat())
    ids = [m.id_hardware for m in maquinas]
    financeiro = compute_financial_summary_by_machine(db, ids, inicio, fim) if ids else {}
    saidas = transacao_summary_by_machine(db, ids, inicio, fim) if ids else {}
    linhas = []
    total = vendas = pelucias = 0
    dados = []
    for maquina in maquinas:
        resumo = financeiro.get(maquina.id_hardware) or {}
        valor = float(resumo.get("faturamento_total") or 0)
        qtd_vendas = int(resumo.get("vendas_count") or 0)
        qtd_saidas = int((saidas.get(maquina.id_hardware) or {}).get("quantidade_saidas") or 0)
        total += valor
        vendas += qtd_vendas
        pelucias += qtd_saidas
        dados.append((valor, maquina, qtd_vendas, qtd_saidas))
    for valor, maquina, qtd_vendas, qtd_saidas in sorted(dados, key=lambda item: -item[0]):
        nome = maquina.nome_local or maquina.id_hardware
        linhas.append(f"• *{nome}*: {formatar_moeda(valor)} · {qtd_vendas} venda(s) · {qtd_saidas} prêmio(s)")
    return {
        "cliente": cliente.nome_empresa,
        "data": hoje.strftime("%d/%m/%Y"),
        "maquinas": "\n".join(linhas) or "Nenhuma máquina cadastrada.",
        "total": formatar_moeda(total),
        "vendas": vendas,
        "pelucias": pelucias,
    }


_LINHAS_EXEMPLO_OFFLINE = (
    "• *Grua Shopping* (ID 1000) – Shopping Centro – sem sinal há 1h 5min\n"
    "• *Grua Entrada* (ID 1001) – Shopping Centro – sem sinal há 1h 2min"
)
VALORES_EXEMPLO = {
    "offline": {
        "cliente": "Cliente Exemplo", "quantidade": 2, "maquinas": _LINHAS_EXEMPLO_OFFLINE,
        "maquina": "Grua Shopping, Grua Entrada", "maquina_id": "1000, 1001", "local": "Shopping Centro",
        "tempo_offline": "1h 5min", "ultimo_sinal": "27/10 14:30",
    },
    "lembrete": {
        "cliente": "Cliente Exemplo", "quantidade": 1,
        "maquinas": "• *Grua Shopping* (ID 1000) – Shopping Centro – sem sinal há 1 dia 1h",
        "maquina": "Grua Shopping", "maquina_id": "1000", "local": "Shopping Centro",
        "tempo_offline": "1 dia 1h", "ultimo_sinal": "26/10 14:30",
    },
    "voltou": {
        "cliente": "Cliente Exemplo", "quantidade": 1,
        "maquinas": "• *Grua Shopping* (ID 1000) – ficou 1h 40min sem sinal",
        "maquina": "Grua Shopping", "maquina_id": "1000", "tempo_offline": "1h 40min",
    },
    "resumo": {
        "cliente": "Cliente Exemplo", "data": "27/10/2026",
        "maquinas": "• *Grua Shopping*: R$ 320,00 · 64 venda(s) · 9 prêmio(s)\n"
        "• *Grua Entrada*: R$ 115,00 · 23 venda(s) · 3 prêmio(s)",
        "total": "R$ 435,00", "vendas": 87, "pelucias": 12,
    },
}


# ---------------------------------------------------------------- worker


def _pegar_trava(db: Session) -> bool:
    """So um processo do backend envia por vez: o UPDATE so pega a linha se a
    trava estiver livre, vencida ou ja for deste processo (atomico no banco)."""
    agora = datetime.utcnow()
    resultado = db.execute(
        update(WhatsappConfig)
        .where(
            WhatsappConfig.id == 1,
            or_(
                WhatsappConfig.worker_lock_ate.is_(None),
                WhatsappConfig.worker_lock_ate < agora,
                WhatsappConfig.worker_lock_id == _PROCESSO_ID,
            ),
        )
        .values(worker_lock_id=_PROCESSO_ID, worker_lock_ate=agora + TRAVA_DURACAO)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return resultado.rowcount == 1


def _soltar_trava(db: Session) -> None:
    db.rollback()
    db.execute(
        update(WhatsappConfig)
        .where(WhatsappConfig.id == 1, WhatsappConfig.worker_lock_id == _PROCESSO_ID)
        .values(worker_lock_ate=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()


def _enviar_para_cliente(
    db: Session, config: WhatsappConfig, cliente: Cliente, tipo: str, texto: str, maquina_ids: list[str]
) -> int:
    """Manda para cada numero do perfil do cliente e registra. Devolve quantos sairam."""
    enviados = 0
    for numero in telefones_do_cliente(db, cliente):
        campos = {
            "cliente_id": cliente.id,
            "maquina_id": ",".join(maquina_ids) or None,
            "tipo": tipo,
            "telefone": numero,
            "mensagem": texto,
            "created_at": datetime.utcnow(),
        }
        try:
            enviar_texto(config, numero, texto)
        except WhatsappErro as exc:
            logger.warning("[whatsapp] falha ao enviar %s para %s: %s", tipo, numero, exc)
            db.add(WhatsappEnvio(status="erro", erro=str(exc)[:500], **campos))
            db.commit()
            continue
        db.add(WhatsappEnvio(status="enviado", **campos))
        db.commit()
        enviados += 1
        time.sleep(PAUSA_ENTRE_MENSAGENS_SEGUNDOS)
    return enviados


def _online(maquina: Maquina, agora: datetime) -> bool:
    return bool(maquina.ultimo_sinal and agora - maquina.ultimo_sinal < ONLINE_SIGNAL_WINDOW)


def verificar_e_enviar(db: Session, agora: datetime | None = None) -> dict:
    """Um ciclo do worker. Devolve contadores de mensagens (usado nos testes/log)."""
    agora = agora or datetime.utcnow()
    resultado = {"offline": 0, "lembrete": 0, "voltou": 0, "resumo": 0, "pulado": None}
    config = db.get(WhatsappConfig, 1)
    if not config or not config.ativo:
        resultado["pulado"] = "desativado"
        if config:
            _registrar_ciclo(db, resultado)
        return resultado
    if not _pegar_trava(db):
        resultado["pulado"] = "outro_processo"
        return resultado
    try:
        _ciclo(db, agora, resultado)
    except Exception as exc:
        logger.exception("[whatsapp] ciclo falhou")
        db.rollback()
        resultado.setdefault("erros", []).append(str(exc)[:200])
    finally:
        _registrar_ciclo(db, resultado)
        _soltar_trava(db)
    return resultado


_PULADO_TEXTO = {
    "desativado": "Envio automatico desativado",
    "sem_clientes": "Nenhum cliente marcado para receber",
    "desconectado": "WhatsApp desconectado: nada foi enviado",
}


def descrever_ciclo(resultado: dict) -> str:
    if resultado.get("erros"):
        return "Erro: " + " | ".join(resultado["erros"])
    if resultado.get("pulado"):
        return _PULADO_TEXTO.get(resultado["pulado"], resultado["pulado"])
    enviados = {t: resultado[t] for t in ("offline", "lembrete", "voltou", "resumo") if resultado.get(t)}
    if not enviados:
        return "OK, nada para enviar agora"
    return "OK, enviado: " + ", ".join(f"{qtd} {tipo}" for tipo, qtd in enviados.items())


def _registrar_ciclo(db: Session, resultado: dict) -> None:
    """Grava como foi o ultimo ciclo para a tela mostrar (diagnostico)."""
    try:
        db.rollback()
        db.execute(
            update(WhatsappConfig)
            .where(WhatsappConfig.id == 1)
            .values(ultimo_ciclo_em=datetime.utcnow(), ultimo_ciclo_status=descrever_ciclo(resultado)[:500])
            .execution_options(synchronize_session=False)
        )
        db.commit()
    except Exception:
        logger.exception("[whatsapp] nao foi possivel registrar o ciclo")
        db.rollback()


def _ciclo(db: Session, agora: datetime, resultado: dict) -> None:
    config = db.get(WhatsappConfig, 1)
    escolhas = {item.cliente_id: item for item in db.query(WhatsappCliente).all()}
    abertos = db.query(WhatsappOfflineAviso).filter(WhatsappOfflineAviso.resolvido_em.is_(None)).all()
    clientes_ids = [cid for cid, e in escolhas.items() if e.ativo or e.resumo_diario]
    if not clientes_ids and not abertos:
        resultado["pulado"] = "sem_clientes"
        return

    try:
        estado = estado_conexao(config)
    except WhatsappErro as exc:
        estado = str(exc)
    if estado != "open":
        logger.warning("[whatsapp] envio pausado, conexao: %s", estado)
        resultado["pulado"] = "desconectado"
        return

    filtros = []
    if clientes_ids:
        filtros.append(Maquina.cliente_id.in_(clientes_ids))
    if abertos:
        filtros.append(Maquina.id_hardware.in_({a.maquina_id for a in abertos}))
    maquinas = db.query(Maquina).options(joinedload(Maquina.dono)).filter(or_(*filtros)).all()
    por_id = {m.id_hardware: m for m in maquinas}

    # Cada rotina isolada: erro numa nao impede as outras.
    etapas = [
        ("voltou", lambda: _avisar_voltou(db, config, agora, escolhas, abertos, por_id, resultado)),
        ("offline", lambda: _avisar_quedas(db, config, agora, escolhas, maquinas, resultado)),
        ("lembrete", lambda: _avisar_lembretes(db, config, agora, escolhas, abertos, por_id, resultado)),
        ("resumo", lambda: _enviar_resumos(db, config, agora, escolhas, maquinas, resultado)),
    ]
    for nome, etapa in etapas:
        try:
            etapa()
        except Exception as exc:
            logger.exception("[whatsapp] etapa %s falhou", nome)
            db.rollback()
            resultado.setdefault("erros", []).append(f"{nome}: {exc}"[:200])


def _avisar_voltou(db, config, agora, escolhas, abertos, por_id, resultado) -> None:
    grupos: dict[int, list] = {}
    for aviso in abertos:
        maquina = por_id.get(aviso.maquina_id)
        if maquina is None or maquina.ultimo_sinal is None:
            aviso.resolvido_em = agora
            continue
        if _online(maquina, agora):
            aviso.resolvido_em = agora
            escolha = escolhas.get(maquina.cliente_id)
            if escolha and escolha.ativo and escolha.recebe_voltou:
                grupos.setdefault(maquina.cliente_id, []).append((maquina, aviso.inicio_offline or aviso.offline_desde))
        elif maquina.ultimo_sinal != aviso.offline_desde:
            # Voltou e caiu de novo entre dois ciclos: e' outra queda.
            aviso.resolvido_em = agora
    db.commit()
    for itens in grupos.values():
        cliente = itens[0][0].dono
        texto = montar_mensagem(modelo_mensagem(config, "voltou"), valores_grupo(cliente, itens, agora, "voltou"))
        if _enviar_para_cliente(db, config, cliente, "voltou", texto, [m.id_hardware for m, _ in itens]):
            resultado["voltou"] += 1


def _avisar_quedas(db, config, agora, escolhas, maquinas, resultado) -> None:
    limite = timedelta(minutes=config.minutos_offline or 60)
    candidatas = [
        m for m in maquinas if m.ultimo_sinal and escolhas.get(m.cliente_id) and escolhas[m.cliente_id].ativo
    ]
    if not candidatas:
        return
    ja_avisadas = set(
        db.query(WhatsappOfflineAviso.maquina_id, WhatsappOfflineAviso.offline_desde).filter(
            WhatsappOfflineAviso.maquina_id.in_([m.id_hardware for m in candidatas])
        )
    )
    grupos: dict[int, list] = {}
    for maquina in candidatas:
        if _online(maquina, agora) or maquina_fechada(maquina, False, agora):
            continue
        if (maquina.id_hardware, maquina.ultimo_sinal) in ja_avisadas:
            continue
        fim_fechamento = fim_ultimo_fechamento(maquina, agora)
        inicio = max(maquina.ultimo_sinal, fim_fechamento) if fim_fechamento else maquina.ultimo_sinal
        if agora - inicio < limite:
            continue
        # Ja estava offline antes de ligar o aviso (geral ou do cliente): nao manda.
        escolha = escolhas[maquina.cliente_id]
        ativado_em = max(filter(None, [config.ativado_em, escolha.ativado_em]), default=None)
        if ativado_em and inicio + limite < ativado_em:
            continue
        grupos.setdefault(maquina.cliente_id, []).append((maquina, inicio))

    for itens in grupos.values():
        cliente = itens[0][0].dono
        # Registra a queda antes de enviar: se o envio travar no meio, nao repete.
        for maquina, inicio in itens:
            db.add(
                WhatsappOfflineAviso(
                    maquina_id=maquina.id_hardware,
                    cliente_id=maquina.cliente_id,
                    offline_desde=maquina.ultimo_sinal,
                    inicio_offline=inicio,
                    avisado_em=agora,
                    lembretes=0,
                )
            )
        db.commit()
        texto = montar_mensagem(modelo_mensagem(config, "offline"), valores_grupo(cliente, itens, agora, "offline"))
        if _enviar_para_cliente(db, config, cliente, "offline", texto, [m.id_hardware for m, _ in itens]):
            resultado["offline"] += 1


def _avisar_lembretes(db, config, agora, escolhas, abertos, por_id, resultado) -> None:
    if not config.lembrete_horas:
        return
    intervalo = timedelta(hours=config.lembrete_horas)
    limite = timedelta(minutes=config.minutos_offline or 60)
    grupos: dict[int, list] = {}
    for aviso in abertos:
        if aviso.resolvido_em is not None:
            continue
        maquina = por_id.get(aviso.maquina_id)
        escolha = escolhas.get(maquina.cliente_id) if maquina else None
        if not escolha or not (escolha.ativo and escolha.recebe_lembrete):
            continue
        if _online(maquina, agora) or maquina_fechada(maquina, False, agora):
            continue
        ultimo = aviso.ultimo_lembrete_em or aviso.avisado_em
        if agora - ultimo < intervalo:
            continue
        # Ponto acabou de abrir: espera o mesmo tempo do primeiro aviso.
        fim_fechamento = fim_ultimo_fechamento(maquina, agora)
        if fim_fechamento and fim_fechamento > ultimo and agora - fim_fechamento < limite:
            continue
        grupos.setdefault(maquina.cliente_id, []).append((maquina, aviso.inicio_offline or aviso.offline_desde, aviso))

    for itens in grupos.values():
        cliente = itens[0][0].dono
        for _, _, aviso in itens:
            aviso.ultimo_lembrete_em = agora
            aviso.lembretes = (aviso.lembretes or 0) + 1
        db.commit()
        pares = [(maquina, inicio) for maquina, inicio, _ in itens]
        texto = montar_mensagem(modelo_mensagem(config, "lembrete"), valores_grupo(cliente, pares, agora, "lembrete"))
        if _enviar_para_cliente(db, config, cliente, "lembrete", texto, [m.id_hardware for m, _ in pares]):
            resultado["lembrete"] += 1


def enviar_resumo_agora(db: Session, cliente: Cliente, agora: datetime | None = None) -> int:
    """Resumo de hoje com os dados reais, fora do horario (botao da tela).
    Nao mexe no controle de "ja enviado hoje"."""
    agora = agora or datetime.utcnow()
    config = obter_config(db)
    if estado_conexao(config) != "open":
        raise WhatsappErro("WhatsApp desconectado. Conecte o numero antes de enviar.")
    maquinas = db.query(Maquina).filter(Maquina.cliente_id == cliente.id).all()
    texto = montar_mensagem(modelo_mensagem(config, "resumo"), valores_resumo(db, cliente, maquinas, agora))
    if not telefones_do_cliente(db, cliente):
        raise WhatsappErro("Cliente sem telefone valido no perfil.")
    enviados = _enviar_para_cliente(db, config, cliente, "resumo", texto, [m.id_hardware for m in maquinas])
    if not enviados:
        erro = (
            db.query(WhatsappEnvio.erro)
            .filter(WhatsappEnvio.cliente_id == cliente.id, WhatsappEnvio.status == "erro")
            .order_by(WhatsappEnvio.id.desc())
            .scalar()
        )
        raise WhatsappErro(erro or "Nao foi possivel enviar o resumo.")
    return enviados


def hora_local_agora(agora: datetime | None = None) -> str:
    return _local(agora or datetime.utcnow()).strftime("%H:%M")


def _enviar_resumos(db, config, agora, escolhas, maquinas, resultado) -> None:
    local = _local(agora)
    hoje = local.date().isoformat()
    hora = local.strftime("%H:%M")
    for cliente_id, escolha in escolhas.items():
        if not escolha.resumo_diario or escolha.resumo_ultimo_dia == hoje:
            continue
        if hora < (escolha.resumo_horario or config.resumo_horario or "22:00"):
            continue
        cliente = db.get(Cliente, cliente_id)
        if cliente is None:
            continue
        do_cliente = [m for m in maquinas if m.cliente_id == cliente_id]
        texto = montar_mensagem(modelo_mensagem(config, "resumo"), valores_resumo(db, cliente, do_cliente, agora))
        # Marca antes de enviar: uma vez por dia, mesmo se falhar no meio.
        escolha.resumo_ultimo_dia = hoje
        db.commit()
        if _enviar_para_cliente(db, config, cliente, "resumo", texto, [m.id_hardware for m in do_cliente]):
            resultado["resumo"] += 1


def run_whatsapp_alert_worker() -> None:
    while True:
        db = SessionLocal()
        try:
            resultado = verificar_e_enviar(db)
            if any(resultado[tipo] for tipo in ("offline", "lembrete", "voltou", "resumo")):
                logger.info("[whatsapp] mensagens enviadas: %s", resultado)
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

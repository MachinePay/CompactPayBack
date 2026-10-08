"""Explica cada queda de uma maquina cruzando o historico em volta dela.

A linha da queda em si (o Last Will do MQTT) nao diz nada alem de "caiu". O
que conta a historia e' o que a placa manda quando volta:

- STATUS|ONLINE (heartbeat de reconexao): uptime, motivo do ultimo reset,
  reboot forcado, contador/motivo de queda de Wi-Fi;
- STATUS|WIFI_DIAG (firmware 2.6.4+): diario de cada evento de Wi-Fi desde a
  ultima conexao (boot + motivo, quedas com codigo, associou, pegou IP...);
- UPDATE_OK / RESET_WIFI_BOTAO logo antes da queda.

Com isso da pra separar falta de energia, travamento da placa, Wi-Fi caindo,
internet do local caindo com o Wi-Fi de pe, e duas placas com o mesmo ID.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# wifi_err_reason_t (ESP-IDF). Texto pensado para quem opera a maquina.
WIFI_DISCONNECT_REASON_LABELS = {
    1: "Motivo nao especificado",
    2: "Autenticacao expirou",
    3: "Roteador desautenticou a placa",
    4: "Associacao expirou por inatividade",
    5: "Roteador cheio (muitos aparelhos conectados)",
    6: "Roteador recusou a placa (nao autenticada)",
    7: "Roteador recusou a placa (nao associada)",
    8: "A placa saiu da rede (desconexao voluntaria)",
    14: "Erro de seguranca (MIC) - senha ou criptografia",
    15: "Senha incorreta ou falha no handshake",
    16: "Falha na troca de chave do grupo",
    23: "Falha de autenticacao 802.1X",
    34: "Muitos pacotes perdidos (interferencia ou sinal ruim)",
    36: "A propria placa encerrou a tentativa de conexao",
    200: "Sinal do roteador sumiu (roteador desligou/reiniciou ou ficou fora de alcance)",
    201: "Rede Wi-Fi nao encontrada",
    202: "Falha de autenticacao (senha incorreta?)",
    203: "Falha de associacao com o roteador",
    204: "Timeout no handshake (senha incorreta?)",
    205: "Falha ao conectar",
    206: "Roteador reiniciou",
    207: "Troca de ponto de acesso (roaming)",
    208: "Roteador pediu para tentar mais tarde",
    210: "Rede encontrada mas com seguranca incompativel",
    211: "Rede encontrada mas com seguranca abaixo do minimo",
    212: "Rede encontrada mas com sinal abaixo do minimo",
}

# esp_reset_reason_t, como vem no B.<n> do WIFI_DIAG.
RESET_REASON_BY_CODE = {
    1: "poweron",
    2: "ext",
    3: "sw",
    4: "panic",
    5: "int_wdt",
    6: "task_wdt",
    7: "wdt",
    8: "deepsleep",
    9: "brownout",
    10: "sdio",
}

FORCED_RESTART_REASON_LABELS = {
    "wifi_offline_5min": "Wi-Fi ficou fora por muito tempo e a placa se reiniciou para tentar de novo",
    "mqtt_offline_5min": "Wi-Fi conectado mas sem falar com o servidor por 5 min; a placa se reiniciou",
    "wifi_no_ap_boot": "Rede Wi-Fi nao encontrada ao ligar; reinicio rapido para tentar de novo",
    "no_successful_publish": "Wi-Fi/servidor diziam que estava tudo bem, mas nada era enviado; a placa se reiniciou",
}

CATEGORIA_LABELS = {
    "energia": "Falta de energia",
    "tensao": "Queda de tensao",
    "travamento": "Placa travou",
    "reinicio_forcado": "Reinicio automatico",
    "reinicio": "Reinicio da placa",
    "atualizacao": "Atualizacao de firmware",
    "configuracao": "Reconfiguracao do Wi-Fi",
    "wifi": "Queda do Wi-Fi",
    "internet": "Internet do local",
    "id_duplicado": "ID duplicado",
    "oscilacao": "Oscilacao rapida",
    "offline": "Ainda offline",
    "desconhecido": "Sem detalhes",
}

_FIELD_RE = re.compile(r"(\w+)=([^\s|]+)")
_FORCED_RESTART_MOTIVO_RE = re.compile(r"reiniciou sozinha apos ficar presa \(motivo: ([^)]+)\)")
_DIAG_EVENT_RE = re.compile(r"^(\d+)\.(\d+)\.([A-Za-z])\.(\d+)$")

# Rajada de quedas de segundos = duas placas com o mesmo ID se derrubando na
# AWS (a AWS IoT so aceita uma conexao por client id).
ID_DUPLICADO_MIN_QUEDAS = 4
ID_DUPLICADO_JANELA = timedelta(seconds=60)
ID_DUPLICADO_MAX_OFFLINE_S = 10
OSCILACAO_MAX_OFFLINE_S = 20
# O aviso de queda (Last Will) so e' publicado pela AWS quando ela desiste da
# conexao: 1,5x o keepalive depois do ultimo pacote. Keepalive 60s desde o
# firmware 2.5.0 (90s de atraso); antes era o padrao da PubSubClient, 15s.
LWT_ATRASO_S_KEEPALIVE_60 = 90
LWT_ATRASO_S_KEEPALIVE_15 = 23
# Placa que acha o roteador em ate isso depois de ligar = roteador estava ligado.
ROTEADOR_LIGADO_ASSOCIOU_EM_S = 5
# Folga ao comparar uptime com o tempo offline para decidir se reiniciou.
REINICIO_FOLGA_S = 180
EVENTO_ANTES_DA_QUEDA = timedelta(minutes=3)


def translate_wifi_reason(code: int | None) -> str:
    if code is None:
        return "--"
    label = WIFI_DISCONNECT_REASON_LABELS.get(code)
    return f"{label} (codigo {code})" if label else f"Codigo {code}"


def parse_fields(descricao: str) -> dict[str, str]:
    return {key: value for key, value in _FIELD_RE.findall(descricao or "")}


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class DiagEvent:
    boot: int
    segundos: int
    tipo: str
    valor: int


def parse_wifi_diag(descricao: str) -> list[DiagEvent]:
    ev = parse_fields(descricao).get("ev", "")
    eventos = []
    for item in ev.split(","):
        match = _DIAG_EVENT_RE.match(item.strip())
        if match:
            eventos.append(
                DiagEvent(int(match.group(1)), int(match.group(2)), match.group(3), int(match.group(4)))
            )
    return eventos


@dataclass
class Evento:
    created_at: datetime
    descricao: str

    @property
    def is_queda(self) -> bool:
        return self.descricao.startswith("Maquina caiu")

    @property
    def is_reinicio_forcado(self) -> bool:
        return self.descricao.startswith("Maquina se reiniciou sozinha")

    def status(self) -> str | None:
        return parse_fields(self.descricao).get("status")


@dataclass
class Diagnostico:
    categoria: str
    motivo: str
    detalhes: list[str] = field(default_factory=list)
    wifi_reason_code: int | None = None
    wifi_disc_count: int | None = None
    # Quando a placa voltou a ligar (so quando ela reiniciou na queda).
    ligou_em: datetime | None = None
    # Quando a maquina provavelmente parou de falar (ver estimar_inicio_queda).
    inicio_estimado: datetime | None = None

    @property
    def categoria_label(self) -> str:
        return CATEGORIA_LABELS.get(self.categoria, self.categoria)


def _motivo_reinicio(reset_reason: str | None, forced_restart: str | None) -> tuple[str, str] | None:
    if reset_reason == "poweron":
        return "energia", "A maquina ficou sem energia (desligada da tomada ou queda de luz) e voltou sozinha"
    if reset_reason == "brownout":
        return "tensao", "Queda de tensao na alimentacao da placa (fonte fraca, mau contato ou pico de consumo)"
    if reset_reason == "panic":
        return "travamento", "A placa travou (erro no firmware) e se reiniciou sozinha"
    if reset_reason in {"task_wdt", "int_wdt", "wdt"}:
        return "travamento", "A placa travou e o watchdog a reiniciou automaticamente"
    if reset_reason in {"sw", "ext"}:
        if forced_restart and forced_restart != "none":
            label = FORCED_RESTART_REASON_LABELS.get(forced_restart, forced_restart)
            return "reinicio_forcado", label
        return "reinicio", "A placa foi reiniciada (comando, atualizacao ou botao de reset)"
    return None


def _versao(fw: str | None) -> tuple[int, int, int] | None:
    match = re.search(r"version_(\d+)\.(\d+)\.(\d+)", fw or "")
    return tuple(int(p) for p in match.groups()) if match else None


def estimar_inicio_queda(
    queda_em: datetime, fw: str | None, ultimo_contato: datetime | None
) -> datetime:
    """Momento aproximado em que a maquina parou de falar: o horario do Last
    Will menos o atraso do keepalive, mas nunca antes do ultimo evento que a
    placa mandou (ela estava viva nesse instante)."""
    versao = _versao(fw)
    atraso = LWT_ATRASO_S_KEEPALIVE_15 if versao and versao < (2, 5, 0) else LWT_ATRASO_S_KEEPALIVE_60
    inicio = queda_em - timedelta(seconds=atraso)
    if ultimo_contato and ultimo_contato > inicio:
        inicio = ultimo_contato
    return min(inicio, queda_em)


def diagnosticar_queda(
    queda: Evento,
    eventos_depois: list[Evento],
    eventos_antes: list[Evento],
    quedas_vizinhas: int,
    reconectou_em: datetime | None,
    firmware_atual: str | None = None,
    ultimo_contato: datetime | None = None,
) -> Diagnostico:
    """eventos_depois: eventos da mesma maquina depois da queda ate a proxima
    queda. eventos_antes: eventos dos minutos anteriores a queda.
    quedas_vizinhas: quantas quedas da mesma maquina houve perto desta.
    firmware_atual: versao atual da maquina, usada so se nenhum heartbeat
    em volta da queda disser qual firmware ela rodava.
    ultimo_contato: ultimo evento que a placa mandou antes da queda."""
    diag = _diagnosticar(
        queda, eventos_depois, eventos_antes, quedas_vizinhas, reconectou_em, firmware_atual, ultimo_contato
    )
    if diag.inicio_estimado is None:
        diag.inicio_estimado = estimar_inicio_queda(
            queda.created_at, _fw_em_volta(eventos_depois, eventos_antes, firmware_atual), ultimo_contato
        )
    return diag


def _fw_em_volta(eventos_depois, eventos_antes, firmware_atual) -> str:
    online = next((e for e in eventos_depois if e.status() == "ONLINE"), None)
    anterior = next((e for e in reversed(eventos_antes) if e.status() == "ONLINE"), None)
    return (
        (parse_fields(online.descricao).get("fw") if online else None)
        or (parse_fields(anterior.descricao).get("fw") if anterior else None)
        or firmware_atual
        or ""
    )


def _diagnosticar(
    queda, eventos_depois, eventos_antes, quedas_vizinhas, reconectou_em, firmware_atual, ultimo_contato
) -> Diagnostico:
    # Tempo entre o aviso de queda e a volta: curtissimo quando a placa
    # reconecta antes de a AWS desistir da sessao antiga (rajada de ID
    # duplicado). Para o resto, usa o tempo real estimado (aviso - keepalive).
    duracao_aviso = (reconectou_em - queda.created_at).total_seconds() if reconectou_em else None
    inicio = estimar_inicio_queda(
        queda.created_at, _fw_em_volta(eventos_depois, eventos_antes, firmware_atual), ultimo_contato
    )
    duracao = (reconectou_em - inicio).total_seconds() if reconectou_em else None

    # Rajada vem antes de tudo: a outra placa com o mesmo ID pode estar sendo
    # configurada pelo botao (caso real 1007, 08/10/2026), e isso nao e' a
    # causa das quedas desta maquina.
    if (
        reconectou_em is not None
        and quedas_vizinhas >= ID_DUPLICADO_MIN_QUEDAS
        and duracao_aviso is not None
        and duracao_aviso <= ID_DUPLICADO_MAX_OFFLINE_S
    ):
        return Diagnostico(
            "id_duplicado",
            "Outra placa esta usando o mesmo ID desta maquina - as duas se derrubam no servidor a cada "
            "poucos segundos. Procure uma placa ligada configurada com este ID e troque o ID dela.",
            [f"{quedas_vizinhas} quedas de segundos no mesmo minuto"],
        )

    for evento in reversed(eventos_antes):
        status = evento.status()
        if status == "UPDATE_OK":
            return Diagnostico("atualizacao", "Reinicio para instalar uma atualizacao de firmware enviada pelo painel")
        if status == "RESET_WIFI_BOTAO":
            return Diagnostico(
                "configuracao",
                "Wi-Fi reconfigurado pelo botao da placa (5 cliques no S3 / portal CONFIG_COMPACTPAY)",
            )

    if reconectou_em is None:
        return Diagnostico("offline", "A maquina ainda nao voltou; o motivo aparece aqui quando ela reconectar")

    online = next((e for e in eventos_depois if e.status() == "ONLINE"), None)
    diag = next((e for e in eventos_depois if e.status() == "WIFI_DIAG"), None)
    online_fields = parse_fields(online.descricao) if online else {}
    diag_eventos = parse_wifi_diag(diag.descricao) if diag else []
    wifi_reason = _int(online_fields.get("wifi_disc_reason"))
    wifi_count = _int(online_fields.get("wifi_disc_count"))
    anterior = next((e for e in reversed(eventos_antes) if e.status() == "ONLINE"), None)
    fw = (
        online_fields.get("fw")
        or (parse_fields(anterior.descricao).get("fw") if anterior else None)
        or firmware_atual
        or ""
    )
    detalhes: list[str] = []

    # 1) A placa reiniciou durante a queda?
    boots = [e for e in diag_eventos if e.tipo == "B"]
    reinicios_forcados = [e for e in diag_eventos if e.tipo == "F"]
    uptime = _int(online_fields.get("uptime"))
    reiniciou = bool(boots) or (
        uptime is not None and duracao is not None and uptime <= duracao + REINICIO_FOLGA_S
    )
    if reiniciou:
        # O primeiro boot do diario e' o que comecou a queda; os seguintes
        # sao reinicios automaticos tentando reconectar.
        reset_reason = RESET_REASON_BY_CODE.get(boots[0].valor) if boots else online_fields.get("reset")
        forced = online_fields.get("forced_restart")
        resultado = _motivo_reinicio(reset_reason, forced)
        ligou_em = _estimar_boot(online, uptime, diag, diag_eventos)
        if resultado and resultado[0] == "energia":
            detalhes.extend(_detalhe_roteador_na_volta(diag_eventos))
        if reinicios_forcados and resultado and resultado[0] in {"energia", "tensao", "travamento"}:
            detalhes.append("Depois disso ainda precisou de reinicios automaticos para reconectar")
        if len(boots) > 1:
            detalhes.append(f"Reiniciou {len(boots) - 1}x sozinha tentando reconectar antes de voltar")
        if resultado:
            categoria, motivo = resultado
            return Diagnostico(categoria, motivo, detalhes, wifi_reason, wifi_count, ligou_em)
        return Diagnostico(
            "reinicio",
            "A placa reiniciou durante a queda",
            detalhes + ([f"Motivo do reset: {reset_reason}"] if reset_reason else []),
            wifi_reason,
            wifi_count,
            ligou_em,
        )

    # 2) Sem reinicio: o Wi-Fi caiu?
    quedas_wifi = [e for e in diag_eventos if e.tipo == "D" and e.valor not in (8, 36)]
    if quedas_wifi:
        codigo = quedas_wifi[0].valor
        viu_rede = [e for e in diag_eventos if e.tipo == "W"]
        if codigo == 201 and viu_rede and viu_rede[0].valor == 0:
            detalhes.append("A varredura confirmou: a rede nao estava no ar")
        return Diagnostico("wifi", f"O Wi-Fi caiu: {translate_wifi_reason(codigo)}", detalhes, codigo, wifi_count)

    firmware_com_diario = diag is not None or _firmware_tem_diario(fw)
    if firmware_com_diario:
        # Firmware novo sempre manda o diario se houve qualquer evento de Wi-Fi.
        # Sem diario (ou so com saidas voluntarias) = o Wi-Fi ficou de pe.
        if duracao is not None and duracao <= OSCILACAO_MAX_OFFLINE_S:
            return Diagnostico(
                "oscilacao",
                "A conexao com o servidor oscilou e voltou em segundos (o Wi-Fi nao caiu)",
                detalhes,
                wifi_reason,
                wifi_count,
            )
        return Diagnostico(
            "internet",
            "Sem acesso ao servidor com o Wi-Fi conectado: a internet do local caiu ou ficou instavel. "
            "Verifique o provedor ou o chip 4G do roteador.",
            detalhes,
            wifi_reason,
            wifi_count,
        )

    # 3) Firmware antigo (sem diario): compara o contador de quedas do Wi-Fi
    # com o ultimo heartbeat registrado antes da queda, no mesmo boot.
    if online and anterior:
        anterior_fields = parse_fields(anterior.descricao)
        count_antes = _int(anterior_fields.get("wifi_disc_count"))
        if count_antes is not None and wifi_count is not None:
            if wifi_count > count_antes:
                return Diagnostico(
                    "wifi", f"O Wi-Fi caiu: {translate_wifi_reason(wifi_reason)}", detalhes, wifi_reason, wifi_count
                )
            return Diagnostico(
                "internet",
                "A internet do local caiu: o Wi-Fi continuou conectado, mas sem acesso ao servidor.",
                detalhes,
                wifi_reason,
                wifi_count,
            )

    if duracao is not None and duracao <= OSCILACAO_MAX_OFFLINE_S:
        return Diagnostico(
            "oscilacao",
            "A conexao com o servidor oscilou e voltou em segundos",
            ["Firmware antigo: nao informa se foi o Wi-Fi ou a internet"],
            wifi_reason,
            wifi_count,
        )
    return Diagnostico(
        "desconhecido",
        "Queda sem detalhes: este firmware nao informa o motivo. Atualize para a versao 2.7.5 ou mais nova.",
        detalhes,
        wifi_reason,
        wifi_count,
    )


def _ultimo_boot(diag_eventos: list[DiagEvent]) -> list[DiagEvent]:
    boots = [e for e in diag_eventos if e.tipo == "B"]
    if not boots:
        return []
    ultimo = boots[-1].boot
    return [e for e in diag_eventos if e.boot == ultimo]


def _estimar_boot(online, uptime, diag, diag_eventos) -> datetime | None:
    """Horario em que a placa ligou: heartbeat de reconexao menos o uptime, ou
    o diario (mandado logo depois de pegar IP) menos o segundo do evento I."""
    if online is not None and uptime is not None:
        return online.created_at - timedelta(seconds=uptime)
    if diag is not None:
        ip = [e for e in _ultimo_boot(diag_eventos) if e.tipo == "I"]
        if ip:
            return diag.created_at - timedelta(seconds=ip[-1].segundos)
    return None


def _detalhe_roteador_na_volta(diag_eventos: list[DiagEvent]) -> list[str]:
    """Na volta de uma falta de energia: o roteador do local tambem tinha
    desligado (queda de luz geral) ou so a maquina ficou sem energia?"""
    eventos = _ultimo_boot(diag_eventos)
    if not eventos:
        return []
    varredura = next((e for e in eventos if e.tipo == "W"), None)
    associou = next((e for e in eventos if e.tipo == "A"), None)
    if varredura is not None and varredura.valor == 0:
        espera = f" (so apareceu {associou.segundos}s depois de a placa ligar)" if associou else ""
        return [
            "O Wi-Fi do local tambem estava fora do ar quando a placa ligou"
            + espera
            + ": provavel queda de luz geral, o roteador tambem desligou"
        ]
    if associou is not None and associou.segundos <= ROTEADOR_LIGADO_ASSOCIOU_EM_S:
        return [
            "O roteador continuou ligado (a placa conectou assim que ligou): "
            "so a maquina ficou sem energia - tomada, disjuntor ou fonte da maquina"
        ]
    return []


def _firmware_tem_diario(fw: str) -> bool:
    match = re.search(r"version_(\d+)\.(\d+)\.(\d+)", fw or "")
    if not match:
        return False
    return tuple(int(p) for p in match.groups()) >= (2, 6, 4)


def diagnosticar_reinicio_forcado(descricao: str) -> Diagnostico:
    match = _FORCED_RESTART_MOTIVO_RE.search(descricao)
    tecnico = match.group(1) if match else None
    motivo = FORCED_RESTART_REASON_LABELS.get(tecnico, tecnico or "Motivo desconhecido")
    return Diagnostico("reinicio_forcado", motivo)


def forced_restart_tecnico(descricao: str) -> str | None:
    match = _FORCED_RESTART_MOTIVO_RE.search(descricao)
    return match.group(1) if match else None

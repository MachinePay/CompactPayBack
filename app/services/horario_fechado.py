"""Horario em que o ponto (loja/local da maquina) fica fechado.

Quando a maquina esta sem sinal dentro desse horario ela aparece como
"fechado" e nao como "offline": a placa nao caiu, o ponto que desligou a
energia/fechou. Fora do horario o offline continua sendo offline.

Formato salvo em Maquina.horario_fechado_json (horario de Brasilia):
    [{"dia": 0, "inicio": "22:00", "fim": "08:00"}, ...]
dia: 0=segunda ... 6=domingo (igual datetime.weekday()).
fim <= inicio atravessa a meia-noite (segunda 22:00 -> terca 08:00).
inicio == fim fecha 24h a partir do inicio ("00:00"-"00:00" = o dia todo).
Sem nada configurado (padrao) o ponto nunca fecha.
"""

import json
import re
from datetime import datetime, timedelta, timezone

BRASILIA_TZ = timezone(timedelta(hours=-3))
_HORA_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _minutos(hora: str) -> int:
    match = _HORA_RE.match(str(hora or "").strip())
    if not match:
        raise ValueError(f"Horario invalido: {hora!r} (use HH:MM)")
    return int(match.group(1)) * 60 + int(match.group(2))


def normalizar_horarios(horarios) -> list[dict]:
    """Valida a lista vinda do painel. Levanta ValueError com mensagem pronta
    para mostrar ao usuario."""
    resultado = []
    for item in horarios or []:
        try:
            dia = int(item.get("dia"))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("Dia da semana invalido")
        if dia < 0 or dia > 6:
            raise ValueError("Dia da semana invalido")
        inicio = str(item.get("inicio") or "").strip()
        fim = str(item.get("fim") or "").strip()
        _minutos(inicio)
        _minutos(fim)
        resultado.append({"dia": dia, "inicio": inicio, "fim": fim})
    resultado.sort(key=lambda item: (item["dia"], item["inicio"]))
    return resultado


def carregar_horarios(maquina) -> list[dict]:
    bruto = getattr(maquina, "horario_fechado_json", None)
    if not bruto:
        return []
    try:
        return normalizar_horarios(json.loads(bruto))
    except (ValueError, TypeError):
        return []


def salvar_horarios(maquina, horarios) -> list[dict]:
    normalizados = normalizar_horarios(horarios)
    maquina.horario_fechado_json = json.dumps(normalizados) if normalizados else None
    return normalizados


def ponto_fechado_agora(maquina, agora_utc: datetime | None = None) -> bool:
    horarios = carregar_horarios(maquina)
    if not horarios:
        return False
    agora_utc = agora_utc or datetime.utcnow()
    local = agora_utc.replace(tzinfo=timezone.utc).astimezone(BRASILIA_TZ)
    dia_hoje = local.weekday()
    dia_ontem = (dia_hoje - 1) % 7
    minuto = local.hour * 60 + local.minute
    for item in horarios:
        inicio = _minutos(item["inicio"])
        fim = _minutos(item["fim"])
        if fim > inicio:
            if item["dia"] == dia_hoje and inicio <= minuto < fim:
                return True
        else:
            # Atravessa a meia-noite: comeca no dia configurado e termina no seguinte.
            if item["dia"] == dia_hoje and minuto >= inicio:
                return True
            if item["dia"] == dia_ontem and minuto < fim:
                return True
    return False


def fim_ultimo_fechamento(maquina, agora_utc: datetime | None = None) -> datetime | None:
    """Quando terminou o fechamento mais recente (UTC naive), ou None se nunca
    fecha. Serve para contar o tempo offline a partir da abertura do ponto, e
    nao de quando a maquina foi desligada na noite anterior."""
    horarios = carregar_horarios(maquina)
    if not horarios:
        return None
    agora_utc = agora_utc or datetime.utcnow()
    agora_local = agora_utc.replace(tzinfo=timezone.utc).astimezone(BRASILIA_TZ)
    hoje = agora_local.replace(hour=0, minute=0, second=0, microsecond=0)
    ultimo = None
    for atras in range(0, 9):
        dia = hoje - timedelta(days=atras)
        for item in horarios:
            if item["dia"] != dia.weekday():
                continue
            inicio = _minutos(item["inicio"])
            fim = _minutos(item["fim"])
            duracao = fim - inicio if fim > inicio else 24 * 60 - inicio + fim
            termino = dia + timedelta(minutes=inicio + duracao)
            if termino <= agora_local and (ultimo is None or termino > ultimo):
                ultimo = termino
    if ultimo is None:
        return None
    return ultimo.astimezone(timezone.utc).replace(tzinfo=None)


def maquina_fechada(maquina, status_online: bool, agora_utc: datetime | None = None) -> bool:
    """Fechado so quando esta sem sinal E dentro do horario de fechamento."""
    return not status_online and ponto_fechado_agora(maquina, agora_utc)

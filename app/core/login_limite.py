"""Limite de tentativas de login (contra quem fica testando senhas).

- Por e-mail: 5 senhas erradas em 15 min bloqueiam aquele e-mail por 5 min.
- Por IP: 20 erros em 15 min bloqueiam o IP por 5 min (pega quem testa
  varios e-mails).
- Login certo zera o contador do e-mail.
- Bloqueado, a senha nem e' conferida (nao da para continuar testando).

Fica na memoria do processo: com mais de uma instancia do backend cada uma
conta separado, e reiniciar zera - suficiente para barrar forca bruta.
"""

import math
import threading
import time
from collections import deque

JANELA_SEGUNDOS = 15 * 60
BLOQUEIO_SEGUNDOS = 5 * 60
MAX_FALHAS_EMAIL = 5
MAX_FALHAS_IP = 20
_MAX_CHAVES = 20_000


class _Contador:
    def __init__(self, maximo: int):
        self.maximo = maximo
        self._falhas: dict[str, deque] = {}
        self._bloqueado_ate: dict[str, float] = {}

    def segundos_bloqueado(self, chave: str, agora: float) -> float:
        ate = self._bloqueado_ate.get(chave)
        if ate is None:
            return 0
        if ate <= agora:
            self._bloqueado_ate.pop(chave, None)
            return 0
        return ate - agora

    def registrar_falha(self, chave: str, agora: float) -> None:
        falhas = self._falhas.setdefault(chave, deque())
        falhas.append(agora)
        while falhas and falhas[0] <= agora - JANELA_SEGUNDOS:
            falhas.popleft()
        if len(falhas) >= self.maximo:
            self._bloqueado_ate[chave] = agora + BLOQUEIO_SEGUNDOS
            falhas.clear()

    def limpar(self, chave: str) -> None:
        self._falhas.pop(chave, None)
        self._bloqueado_ate.pop(chave, None)

    def podar(self, agora: float) -> None:
        if len(self._falhas) + len(self._bloqueado_ate) < _MAX_CHAVES:
            return
        for chave in [c for c, f in self._falhas.items() if not f or f[-1] <= agora - JANELA_SEGUNDOS]:
            del self._falhas[chave]
        for chave in [c for c, ate in self._bloqueado_ate.items() if ate <= agora]:
            del self._bloqueado_ate[chave]


_lock = threading.Lock()
_por_email = _Contador(MAX_FALHAS_EMAIL)
_por_ip = _Contador(MAX_FALHAS_IP)


def _email(email: str | None) -> str:
    return str(email or "").strip().lower()


def minutos_bloqueado(email: str | None, ip: str | None) -> int:
    """0 se pode tentar; senao, quantos minutos faltam."""
    agora = time.monotonic()
    with _lock:
        segundos = max(_por_email.segundos_bloqueado(_email(email), agora), _por_ip.segundos_bloqueado(ip or "", agora))
    return math.ceil(segundos / 60) if segundos else 0


def registrar_falha(email: str | None, ip: str | None) -> None:
    agora = time.monotonic()
    with _lock:
        _por_email.registrar_falha(_email(email), agora)
        if ip:
            _por_ip.registrar_falha(ip, agora)
        _por_email.podar(agora)
        _por_ip.podar(agora)


def registrar_sucesso(email: str | None) -> None:
    with _lock:
        _por_email.limpar(_email(email))


def ip_do_pedido(request) -> str | None:
    # No Render a requisicao chega por proxy, que ACRESCENTA o IP real no fim
    # do X-Forwarded-For. Os primeiros podem ter sido inventados por quem
    # chamou (para fugir do limite por IP) - por isso vale o ultimo.
    encaminhado = request.headers.get("x-forwarded-for")
    if encaminhado:
        return encaminhado.split(",")[-1].strip() or None
    return request.client.host if request.client else None


def zerar() -> None:
    """So para testes."""
    global _por_email, _por_ip
    with _lock:
        _por_email = _Contador(MAX_FALHAS_EMAIL)
        _por_ip = _Contador(MAX_FALHAS_IP)

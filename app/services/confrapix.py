"""Cliente da API Confrapix (Pix dinamico), usado pelo banco "confrapag_sdk".

Token: sempre o do proprio cliente (Cliente.confrapix_token), nunca um global.
Endereco: CONFRAPIX_BASE_URL (producao https://api.confrapix.com.br,
sandbox https://sandbox.confrapix.com.br).
"""
import json
import urllib.error
import urllib.request

from fastapi import HTTPException

from app.core.config import settings

STATUS_PAGO = {"succeeded"}
STATUS_FINAIS_SEM_PAGAMENTO = {"canceled", "cancelled", "expired", "failed", "refunded"}  # doc: processing, succeeded, canceled, failed, expired, refunded


def _url(caminho: str) -> str:
    return settings.CONFRAPIX_BASE_URL.rstrip("/") + caminho


def _request(method: str, caminho: str, token: str, body: dict | None = None, timeout: float = 20) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {token}"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(_url(caminho), data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        corpo = exc.read().decode("utf-8", errors="ignore")
        # Sem o corpo inteiro no log: pode ter CPF/nome do pagador.
        print(f"[Confrapix] {method} {caminho} falhou ({exc.code})")
        mensagem = "Confrapix recusou a operacao"
        try:
            mensagem = json.loads(corpo).get("message") or mensagem
        except (ValueError, AttributeError):
            pass
        if exc.code in (401, 403):
            mensagem = "Token Confrapix invalido ou sem permissao"
        raise HTTPException(status_code=502, detail=f"Confrapix: {mensagem}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail="Confrapix fora do ar ou sem resposta") from exc


def _transacao(resposta: dict) -> dict:
    """A API embrulha em {"status", "success", "message", "transaction": {...}}."""
    for chave in ("transaction", "data"):
        valor = resposta.get(chave)
        if isinstance(valor, dict):
            return valor
    return resposta


def criar_cobranca(token: str, valor: float, descricao: str, expiracao: str, callback_url: str | None) -> dict:
    body = {"amount": round(float(valor), 2), "description": descricao, "expiration_date": expiracao}
    if callback_url:
        body["callback_url"] = callback_url
    transacao = _transacao(_request("POST", "/api/transaction-ec/store", token, body))
    if not transacao.get("id") or not (transacao.get("pix") or {}).get("code"):
        raise HTTPException(status_code=502, detail="Confrapix nao devolveu o codigo Pix")
    return transacao


def consultar(token: str, transacao_id: str) -> dict:
    return _transacao(_request("GET", f"/api/transaction-ec/show/{transacao_id}", token))


def cancelar(token: str, transacao_id: str) -> dict:
    return _transacao(_request("PUT", f"/api/transaction-ec/cancel/{transacao_id}", token, {}))

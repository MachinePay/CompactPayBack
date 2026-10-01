import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException

API = "https://api.sumup.com"

_reader_status_cache: dict[str, tuple[float, dict]] = {}
_READER_STATUS_CACHE_SECONDS = 30


def _su_error_message(status_code: int, error_body: str) -> str:
    try:
        data = json.loads(error_body) if error_body else {}
    except json.JSONDecodeError:
        data = {"raw": error_body}
    message = str(data.get("message") or data.get("error_message") or data.get("error") or data.get("raw") or "erro sem detalhe").strip()
    return f"SumUp retornou erro {status_code}: {message}"


def su_request(method: str, url: str, api_key: str, body: dict | None = None, headers: dict | None = None):
    req_headers = {"Content-Type": "application/json"}
    if api_key:
        req_headers["Authorization"] = f"Bearer {api_key}"
    if headers:
        req_headers.update(headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        print(f"[SumUp] {method} {url} falhou ({exc.code}): {error_body or 'erro sem detalhe'}")
        raise HTTPException(status_code=502, detail=_su_error_message(exc.code, error_body)) from exc


def get_sumup_merchant_code(access_token: str) -> str:
    # Resolve o merchant_code sozinho a partir so da API key, igual o
    # get_mp_user_id ja faz pro Mercado Pago - evita o cliente ter que ir
    # procurar esse codigo na tela de configuracoes do app SumUp.
    data = su_request("GET", f"{API}/v0.1/me", access_token)
    merchant_code = (data.get("merchant_profile") or {}).get("merchant_code")
    if not merchant_code:
        raise HTTPException(status_code=502, detail="SumUp nao retornou merchant_code para esta API key")
    return str(merchant_code)


def list_readers(access_token: str, merchant_code: str) -> list[dict]:
    data = su_request("GET", f"{API}/v0.1/merchants/{merchant_code}/readers", access_token)
    items = data if isinstance(data, list) else (data.get("items") or data.get("readers") or data.get("data") or [])
    result = []
    for item in items:
        reader_id = item.get("id") or item.get("reader_id")
        device_identifier = (item.get("device") or {}).get("identifier")
        name = item.get("name") or device_identifier
        result.append({
            "id": reader_id,
            "name": name,
            "label": f"{name} ({reader_id})" if name else reader_id,
            # Serial fisico do reader - usado pra casar com device_info das
            # transacoes do historico (ver build_reader_device_map).
            "device_identifier": device_identifier,
        })
    return result


def build_reader_device_map(access_token: str, merchant_code: str) -> dict[str, str]:
    """device_identifier (serial do reader) -> reader_id, pra casar transacoes
    do historico (que trazem device_info, nao reader_id) com o reader que
    processou. So vale a pena chamar quando o cliente tem mais de uma maquina
    SumUp - com uma so, nao ha ambiguidade a resolver (ver sumup_poller.py)."""
    mapa: dict[str, str] = {}
    for reader in list_readers(access_token, merchant_code):
        if reader.get("device_identifier") and reader.get("id"):
            mapa[str(reader["device_identifier"])] = str(reader["id"])
    return mapa


def create_reader_checkout(
    access_token: str,
    merchant_code: str,
    reader_id: str,
    amount: float,
    client_transaction_id: str,
    description: str | None = None,
    currency: str = "BRL",
    return_url: str | None = None,
) -> dict:
    body = {
        "total_amount": {
            "currency": currency,
            "minor_unit": 2,
            "value": round(amount * 100),
        },
        "client_transaction_id": client_transaction_id,
    }
    if description:
        body["description"] = description
    if return_url:
        body["return_url"] = return_url

    data = su_request(
        "POST",
        f"{API}/v0.1/merchants/{merchant_code}/readers/{reader_id}/checkout",
        access_token,
        body=body,
    )
    payload = data.get("data") or data
    checkout_id = payload.get("checkout_id") or payload.get("id")
    if not checkout_id:
        raise HTTPException(status_code=502, detail="SumUp nao retornou checkout_id ao criar a cobranca")
    return {
        "checkout_id": checkout_id,
        "client_transaction_id": payload.get("client_transaction_id") or client_transaction_id,
    }


def get_reader_checkout(access_token: str, merchant_code: str, reader_id: str, checkout_id: str) -> dict:
    data = su_request(
        "GET",
        f"{API}/v0.1/merchants/{merchant_code}/readers/{reader_id}/checkout/{checkout_id}",
        access_token,
    )
    return data.get("data") or data


def list_recent_transactions(access_token: str, merchant_code: str, changes_since: str | None = None) -> list[dict]:
    # Usado pelo polling (sumup_poller.py) pra detectar pagamentos feitos
    # DIRETO na maquininha (standalone, cliente digita o valor nela mesma) -
    # esses NUNCA disparam o webhook por checkout (so cobrancas criadas pela
    # nossa propria API tem isso). changes_since filtra so o que mudou desde
    # a ultima consulta, pra nao reprocessar o historico inteiro toda vez.
    #
    # IMPORTANTE (confirmado na doc oficial): este endpoint de HISTORICO nao
    # devolve device_info - esse campo so existe no retrieve de uma transacao
    # especifica (ver get_transaction_details). Pra casar a transacao com o
    # reader correto quando o cliente tem mais de uma maquina, o poller
    # precisa chamar get_transaction_details pra cada transacao nova.
    #
    # NAO filtramos por payment_types[] aqui - testado em producao (transacao
    # real confirmada como concluida no app da SumUp) e o filtro
    # "payment_types[]=POS" fazia esse endpoint devolver ZERO resultados,
    # mesmo pra pagamento feito na maquininha fisica. Ou o valor do enum nao e'
    # exatamente esse pra transacao standalone, ou a API nao aceita esse
    # parametro do jeito documentado. A seguranca contra transacao de outra
    # origem (ex.: ecommerce na mesma conta) fica por conta da verificacao de
    # device/reader no poller, nao desse filtro.
    params = {
        "statuses[]": "SUCCESSFUL",
        "limit": "100",
        "order": "ascending",
    }
    if changes_since:
        params["changes_since"] = changes_since
    query = urllib.parse.urlencode(params)
    url = f"{API}/v2.1/merchants/{merchant_code}/transactions/history?{query}"
    logging.info("[SumUp] consultando historico: %s", url)
    data = su_request("GET", url, access_token)
    items = data.get("items") if isinstance(data, dict) else data
    logging.info("[SumUp] resposta bruta do historico (ate 2000 chars): %s", json.dumps(data, default=str)[:2000])
    return items or []


def get_transaction_details(access_token: str, merchant_code: str, transaction_id: str) -> dict:
    # Unico endpoint que devolve device_info - usado pelo poller so quando ha
    # ambiguidade (cliente com mais de uma maquina) pra descobrir qual reader
    # fisico processou a transacao (ver list_recent_transactions).
    query = urllib.parse.urlencode({"id": transaction_id})
    data = su_request("GET", f"{API}/v2.1/merchants/{merchant_code}/transactions?{query}", access_token)
    result = data.get("data") or data
    logging.info(
        "[SumUp] resposta bruta do retrieve de transacao %s (ate 2000 chars): %s",
        transaction_id, json.dumps(result, default=str)[:2000],
    )
    return result


def create_sumup_refund(access_token: str, transaction_id: str) -> None:
    # Endpoint de refund da SumUp usa o transaction_id (nao o checkout_id) -
    # ver get_reader_checkout, que devolve esse campo assim que o pagamento
    # e' concluido com sucesso.
    su_request("POST", f"{API}/v0.1/me/refund/{transaction_id}", access_token, body={})


def terminate_reader_checkout(access_token: str, merchant_code: str, reader_id: str) -> None:
    su_request(
        "POST",
        f"{API}/v0.1/merchants/{merchant_code}/readers/{reader_id}/terminate",
        access_token,
    )


def get_reader_status(access_token: str, merchant_code: str, reader_id: str) -> dict:
    cache_key = f"{merchant_code}:{reader_id}"
    cached = _reader_status_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < _READER_STATUS_CACHE_SECONDS:
        return cached[1]
    try:
        data = su_request(
            "GET",
            f"{API}/v0.1/merchants/{merchant_code}/readers/{reader_id}/status",
            access_token,
        )
        status_raw = str((data.get("data") or data).get("status") or "").strip().lower()
        result = {
            "status": status_raw or "unknown",
            "online": status_raw in {"online", "idle", "ready"},
        }
    except HTTPException as exc:
        print(f"[SumUp] status do reader indisponivel reader_id={reader_id}: {exc.detail}")
        result = {"status": "unavailable", "online": False}
    _reader_status_cache[cache_key] = (time.monotonic(), result)
    return result


def get_active_terminal_for_machine(cliente, maquina) -> dict:
    access_token = ((getattr(cliente, "sumup_api_key", None) or "") if cliente else "").strip()
    merchant_code = ((getattr(cliente, "sumup_merchant_code", None) or "") if cliente else "").strip()
    reader_id = (getattr(maquina, "sumup_reader_id", None) or "").strip()
    if not access_token or not merchant_code or not reader_id:
        return {"status": "not_linked", "online": False, "terminal_id": None}
    status = get_reader_status(access_token, merchant_code, reader_id)
    return {
        "status": status["status"],
        "online": status["online"],
        "terminal_id": reader_id,
    }

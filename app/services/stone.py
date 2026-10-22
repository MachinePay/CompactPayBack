import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException

from app.core.config import settings

# Connect Stone (maquininha Stone) e' operado pela API Core v5 da Pagar.me.
# Doc: https://connect-stone.stone.com.br/llms.txt
API = "https://api.pagar.me/core/v5"

# Estorno de cobranca feita na maquininha NAO tem endpoint na API - a doc do
# Connect Stone manda fazer na propria maquininha ou no Portal Stone.
STONE_SEM_ESTORNO_API = (
    "A Stone nao permite estorno pela API: faca o estorno na propria maquininha "
    "ou no Portal Stone. O CompactPay marca como estornado quando a Stone avisar (charge.refunded)."
)


class StoneAPIError(HTTPException):
    """HTTPException 502 que guarda o status real devolvido pela Stone - o
    webhook precisa diferenciar 401/404 (chave de outro cliente) de erro real."""

    def __init__(self, provider_status: int, detail: str):
        super().__init__(status_code=502, detail=detail)
        self.provider_status = provider_status


def _stone_error_message(status_code: int, error_body: str) -> str:
    try:
        data = json.loads(error_body) if error_body else {}
    except json.JSONDecodeError:
        data = {"raw": error_body}
    if status_code == 401:
        return "Stone recusou a Secret Key (401) - confira a chave sk_ cadastrada no usuario"
    message = str(data.get("message") or data.get("raw") or "erro sem detalhe").strip()
    return f"Stone retornou erro {status_code}: {message}"


def stone_request(method: str, path: str, secret_key: str, body: dict | None = None, service_referer_name: str | None = None):
    # Auth da Pagar.me: Basic com a Secret Key como usuario e senha vazia.
    token = base64.b64encode(f"{secret_key}:".encode("utf-8")).decode("ascii")
    headers = {"Content-Type": "application/json", "Authorization": f"Basic {token}"}
    if service_referer_name:
        headers["ServiceRefererName"] = service_referer_name
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{API}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        print(f"[Stone] {method} {path} falhou ({exc.code}): {error_body or 'erro sem detalhe'}")
        raise StoneAPIError(exc.code, _stone_error_message(exc.code, error_body)) from exc


def stone_credentials(cliente) -> tuple[str, str | None]:
    """(secret_key, service_referer_name) do cliente, com fallback pro .env."""
    secret_key = ((getattr(cliente, "stone_secret_key", None) or "") if cliente else "").strip() or settings.STONE_SECRET_KEY.strip()
    referer = ((getattr(cliente, "stone_service_referer_name", None) or "") if cliente else "").strip() or settings.STONE_SERVICE_REFERER_NAME.strip()
    return secret_key, (referer or None)


def test_secret_key(secret_key: str, service_referer_name: str | None = None) -> None:
    stone_request("GET", "/orders?page=1&size=1", secret_key, service_referer_name=service_referer_name)


def get_charge(secret_key: str, charge_id: str, service_referer_name: str | None = None) -> dict:
    return stone_request("GET", f"/charges/{urllib.parse.quote(charge_id, safe='')}", secret_key, service_referer_name=service_referer_name)


def list_paid_charges(secret_key: str, created_since: str | None, service_referer_name: str | None = None, max_pages: int = 5) -> list[dict]:
    charges: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {"status": "paid", "page": page, "size": 100}
        if created_since:
            params["created_since"] = created_since
        data = stone_request("GET", f"/charges?{urllib.parse.urlencode(params)}", secret_key, service_referer_name=service_referer_name)
        items = data.get("data") or []
        charges.extend(items)
        if len(items) < 100:
            break
    return charges


def normalize_serial(value) -> str:
    return str(value or "").strip().upper()


def extract_terminal_serial(charge: dict) -> str:
    """Serial da maquininha que processou a cobranca. Na doc vem em
    metadata.terminal_serial_number; olhamos tambem a ultima transacao por
    seguranca (o formato real pode variar entre versoes do app da maquininha)."""
    metadata = charge.get("metadata") or {}
    last_transaction = charge.get("last_transaction") or {}
    for value in (
        metadata.get("terminal_serial_number"),
        (last_transaction.get("metadata") or {}).get("terminal_serial_number"),
        last_transaction.get("terminal_serial_number"),
        (last_transaction.get("poi") or {}).get("serial_number"),
    ):
        serial = normalize_serial(value)
        if serial:
            return serial
    return ""


def charge_amount(charge: dict) -> float:
    cents = charge.get("paid_amount") or charge.get("amount") or 0
    return round(float(cents) / 100, 2)


def charge_payment_type(charge: dict) -> str | None:
    metadata = charge.get("metadata") or {}
    method = str(charge.get("payment_method") or "").strip().lower()
    if method == "pix":
        return "pix"
    funding = str(metadata.get("account_funding_source") or "").strip().lower()
    return funding or method or None


def charge_card_brand(charge: dict) -> str | None:
    metadata = charge.get("metadata") or {}
    brand = str(metadata.get("scheme_name") or ((charge.get("last_transaction") or {}).get("card") or {}).get("brand") or "").strip()
    return brand or None

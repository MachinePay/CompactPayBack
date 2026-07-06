import logging
import threading
import time
from uuid import uuid4
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.core.config import settings
from app.services.mqtt_worker import start_mqtt_worker
from app.api.v1.routes import router as api_router
from app.db.base import Base
from app.db.session import engine
from sqlalchemy import inspect, text
import app.models.models  # noqa: F401
import app.models.produto  # noqa: F401

app = FastAPI()

def _normalize_origin(origin: str) -> str:
    return origin.strip().rstrip("/")


def _build_allowed_origins() -> list[str]:
    extra_origins = settings.CORS_ALLOWED_ORIGINS.split(",")
    origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://compactpay.com.br",
        "https://compactpay.vercel.app",
        settings.FRONTEND_URL,
        *extra_origins,
    ]
    return list(dict.fromkeys(origin for origin in (_normalize_origin(item) for item in origins) if origin))


ALLOWED_ORIGINS = _build_allowed_origins()
logging.info("CORS allow_origins configurado: %s", ", ".join(ALLOWED_ORIGINS))

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"^https://compact-pay-front(-[a-z0-9-]+)*\.vercel\.app$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")

@app.middleware("http")
async def log_requests(request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    request.state.request_id = request_id
    try:
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start_time) * 1000
        response.headers["X-Request-ID"] = request_id
        logging.info(
            "HTTP request_id=%s %s %s status=%s duration_ms=%.2f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response
    except Exception:
        duration_ms = (time.perf_counter() - start_time) * 1000
        logging.exception(
            "HTTP request_id=%s %s %s status=error duration_ms=%.2f",
            request_id,
            request.method,
            request.url.path,
            duration_ms,
        )
        raise


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", str(uuid4()))
    logging.exception("Unhandled exception request_id=%s path=%s", request_id, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Erro interno no servidor.",
            "request_id": request_id,
        },
        headers={"X-Request-ID": request_id},
    )


def run_mqtt():
    start_mqtt_worker()

@app.on_event("startup")
def startup_event():
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        inspector = inspect(connection)
        cliente_columns = {column["name"] for column in inspector.get_columns("clientes")}
        for column_name in [
            "telefone",
            "cpf",
            "cnpj",
            "endereco_rua",
            "endereco_numero",
            "endereco_cidade",
            "endereco_estado",
            "endereco_latitude",
            "endereco_longitude",
            "cliente_mercado_pago",
            "cliente_pagbank",
            "cliente_s6pay",
            "mp_public_key",
            "mp_access_token",
            "mp_client_id",
            "mp_client_secret",
            "mp_user_id",
            "mp_refresh_token",
            "mp_token_expires_at",
            "mp_live_mode",
            "mp_scope",
            "mp_pos_category",
            "mp_store_id",
            "mp_store_external_id",
        ]:
            if column_name not in cliente_columns:
                column_type = "BOOLEAN" if column_name in {"mp_live_mode", "cliente_mercado_pago", "cliente_pagbank", "cliente_s6pay"} else "TIMESTAMP" if column_name == "mp_token_expires_at" else "FLOAT" if column_name in {"endereco_latitude", "endereco_longitude"} else "INTEGER" if column_name == "mp_pos_category" else "VARCHAR"
                connection.execute(text(f"ALTER TABLE clientes ADD COLUMN {column_name} {column_type}"))

        usuario_columns = {column["name"] for column in inspector.get_columns("usuarios")}
        for column_name in [
            "nome",
            "telefone",
            "cpf",
            "cnpj",
            "endereco_rua",
            "endereco_numero",
            "endereco_cidade",
            "endereco_estado",
            "endereco_latitude",
            "endereco_longitude",
            "cliente_mercado_pago",
            "cliente_pagbank",
            "cliente_s6pay",
            "mp_public_key",
            "mp_access_token",
            "mp_client_id",
            "mp_client_secret",
            "mp_user_id",
            "mp_refresh_token",
            "mp_token_expires_at",
            "mp_live_mode",
            "mp_scope",
            "mp_pos_category",
            "mp_store_id",
            "mp_store_external_id",
        ]:
            if column_name not in usuario_columns:
                column_type = "BOOLEAN" if column_name in {"mp_live_mode", "cliente_mercado_pago", "cliente_pagbank", "cliente_s6pay"} else "TIMESTAMP" if column_name == "mp_token_expires_at" else "FLOAT" if column_name in {"endereco_latitude", "endereco_longitude"} else "INTEGER" if column_name == "mp_pos_category" else "VARCHAR"
                connection.execute(text(f"ALTER TABLE usuarios ADD COLUMN {column_name} {column_type}"))

        maquina_columns = {column["name"] for column in inspector.get_columns("maquinas")}
        if "localizacao" not in maquina_columns:
            connection.execute(text("ALTER TABLE maquinas ADD COLUMN localizacao VARCHAR"))
        if "banco_pagamento" not in maquina_columns:
            connection.execute(text("ALTER TABLE maquinas ADD COLUMN banco_pagamento VARCHAR"))
        for column_name in ["wifi_rssi", "wifi_quality"]:
            if column_name not in maquina_columns:
                connection.execute(text(f"ALTER TABLE maquinas ADD COLUMN {column_name} INTEGER"))
        for column_name in [
            "mp_store_id",
            "mp_store_external_id",
            "mp_pos_id",
            "mp_pos_external_id",
            "mp_qr_image",
            "firmware_version",
            "firmware_target_version",
            "firmware_update_status",
            "firmware_update_command_id",
            "firmware_update_url",
        ]:
            if column_name not in maquina_columns:
                connection.execute(text(f"ALTER TABLE maquinas ADD COLUMN {column_name} VARCHAR"))
        for column_name in [
            "firmware_updated_at",
            "firmware_update_requested_at",
            "firmware_update_started_at",
            "firmware_update_finished_at",
        ]:
            if column_name not in maquina_columns:
                connection.execute(text(f"ALTER TABLE maquinas ADD COLUMN {column_name} TIMESTAMP"))
        if "firmware_versions" not in inspector.get_table_names():
            connection.execute(text("""
                CREATE TABLE firmware_versions (
                    id INTEGER PRIMARY KEY,
                    nome VARCHAR NOT NULL,
                    url_bin VARCHAR NOT NULL,
                    observacao VARCHAR,
                    ativo BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                )
            """))
        else:
            firmware_columns = {column["name"] for column in inspector.get_columns("firmware_versions")}
            for column_name in ["nome", "url_bin", "observacao"]:
                if column_name not in firmware_columns:
                    nullable = "" if column_name == "observacao" else " NOT NULL DEFAULT ''"
                    connection.execute(text(f"ALTER TABLE firmware_versions ADD COLUMN {column_name} VARCHAR{nullable}"))
            if "ativo" not in firmware_columns:
                connection.execute(text("ALTER TABLE firmware_versions ADD COLUMN ativo BOOLEAN NOT NULL DEFAULT TRUE"))
            for column_name in ["created_at", "updated_at"]:
                if column_name not in firmware_columns:
                    connection.execute(text(f"ALTER TABLE firmware_versions ADD COLUMN {column_name} TIMESTAMP"))
        historico_columns = {column["name"] for column in inspector.get_columns("historico_operacoes")}
        for column_name in [
            "provider",
            "provider_payment_id",
            "payment_type",
            "card_brand",
            "bank_name",
            "pulse_status",
            "command_id",
        ]:
            if column_name not in historico_columns:
                connection.execute(text(f"ALTER TABLE historico_operacoes ADD COLUMN {column_name} VARCHAR"))
        if "refunded_at" not in historico_columns:
            connection.execute(text("ALTER TABLE historico_operacoes ADD COLUMN refunded_at TIMESTAMP"))
        vendas_columns = {column["name"] for column in inspector.get_columns("vendas_pagamentos")}
        if "command_id" not in vendas_columns:
            connection.execute(text("ALTER TABLE vendas_pagamentos ADD COLUMN command_id VARCHAR"))
    if settings.START_MQTT_WORKER:
        mqtt_thread = threading.Thread(target=run_mqtt, daemon=True)
        mqtt_thread.start()
    else:
        logging.info("MQTT worker desativado por START_MQTT_WORKER=false")

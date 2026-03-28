import threading
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.services.mqtt_worker import start_mqtt_worker
from app.api.v1.routes import router as api_router
from app.db.base import Base
from app.db.session import engine
from sqlalchemy import inspect, text
import app.models.models  # noqa: F401
import app.models.produto  # noqa: F401

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://compactpay.com.br",
        "https://compactpay.vercel.app",
    ],
    allow_origin_regex=r"^https://compact-pay-front(-[a-z0-9-]+)*\.vercel\.app$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")

def run_mqtt():
    start_mqtt_worker()

@app.on_event("startup")
def startup_event():
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        inspector = inspect(connection)
        maquina_columns = {column["name"] for column in inspector.get_columns("maquinas")}
        if "localizacao" not in maquina_columns:
            connection.execute(text("ALTER TABLE maquinas ADD COLUMN localizacao VARCHAR"))
    mqtt_thread = threading.Thread(target=run_mqtt, daemon=True)
    mqtt_thread.start()

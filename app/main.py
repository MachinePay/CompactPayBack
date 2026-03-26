import threading
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.services.mqtt_worker import start_mqtt_worker
from app.api.v1.routes import router as api_router

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
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
    mqtt_thread = threading.Thread(target=run_mqtt, daemon=True)
    mqtt_thread.start()

import threading
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.services.mqtt_worker import start_mqtt_worker

app = FastAPI()

# CORS para domínio da Vercel
origins = [
    "https://*.vercel.app",
    "https://vercel.app"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Importa rotas
from app.api.v1 import routes
app.include_router(routes.router, prefix="/api/v1")

def run_mqtt():
    start_mqtt_worker()

@app.on_event("startup")
def startup_event():
    mqtt_thread = threading.Thread(target=run_mqtt, daemon=True)
    mqtt_thread.start()

from pydantic_settings import BaseSettings
import os

class Settings(BaseSettings):
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/compactpay")
    MQTT_BROKER_URL: str = os.getenv("MQTT_BROKER_URL", "broker.hivemq.com")
    MQTT_BROKER_PORT: int = int(os.getenv("MQTT_BROKER_PORT", 1883))
    MQTT_USERNAME: str = os.getenv("MQTT_USERNAME", "")
    MQTT_PASSWORD: str = os.getenv("MQTT_PASSWORD", "")
    MP_ACCESS_TOKEN: str = os.getenv("MP_ACCESS_TOKEN", "")
    MP_WEBHOOK_SECRET: str = os.getenv("MP_WEBHOOK_SECRET", "")
    MP_APP_ID: str = os.getenv("MP_APP_ID", os.getenv("MP_CLIENT_ID", ""))
    MP_CLIENT_SECRET: str = os.getenv("MP_CLIENT_SECRET", "")
    MP_OAUTH_REDIRECT_URI: str = os.getenv("MP_OAUTH_REDIRECT_URI", "")
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:5173")
    MP_DEFAULT_STORE_STREET_NAME: str = os.getenv("MP_DEFAULT_STORE_STREET_NAME", "Rua CompactPay")
    MP_DEFAULT_STORE_STREET_NUMBER: str = os.getenv("MP_DEFAULT_STORE_STREET_NUMBER", "0")
    MP_DEFAULT_STORE_CITY_NAME: str = os.getenv("MP_DEFAULT_STORE_CITY_NAME", "São Paulo")
    MP_DEFAULT_STORE_STATE_NAME: str = os.getenv("MP_DEFAULT_STORE_STATE_NAME", "São Paulo")
    MP_DEFAULT_STORE_LATITUDE: float = float(os.getenv("MP_DEFAULT_STORE_LATITUDE", "-23.55052"))
    MP_DEFAULT_STORE_LONGITUDE: float = float(os.getenv("MP_DEFAULT_STORE_LONGITUDE", "-46.633308"))
    MP_DEFAULT_POS_CATEGORY: int = int(os.getenv("MP_DEFAULT_POS_CATEGORY", "621102"))

settings = Settings()

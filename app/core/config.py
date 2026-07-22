import os
import ssl

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_VERSION: str = os.getenv("APP_VERSION", "dev")
    APP_REVISION: str = os.getenv("APP_REVISION", "")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/compactpay")
    MQTT_BROKER_URL: str = os.getenv("MQTT_BROKER_URL", "a11hfacfencseq-ats.iot.us-west-2.amazonaws.com")
    MQTT_BROKER_PORT: int = int(os.getenv("MQTT_BROKER_PORT", 8883))
    MQTT_USE_TLS: bool = os.getenv("MQTT_USE_TLS", "true").lower() == "true"
    MQTT_CLIENT_ID: str = os.getenv("MQTT_CLIENT_ID", "Backend_CompactPay_Worker")
    MQTT_PUBLISH_CLIENT_ID_PREFIX: str = os.getenv("MQTT_PUBLISH_CLIENT_ID_PREFIX", "Backend_CompactPay_Pub")
    AWS_CA_PATH: str = os.getenv("AWS_CA_PATH", "certs/AmazonRootCA1.pem")
    AWS_CERT_PATH: str = os.getenv("AWS_CERT_PATH", "certs/backend-certificate.pem.crt")
    AWS_KEY_PATH: str = os.getenv("AWS_KEY_PATH", "certs/backend-private.pem.key")
    MQTT_USERNAME: str = os.getenv("MQTT_USERNAME", "")
    MQTT_PASSWORD: str = os.getenv("MQTT_PASSWORD", "")
    START_MQTT_WORKER: bool = os.getenv("START_MQTT_WORKER", "true").lower() == "true"
    START_COMMAND_QUEUE_WORKER: bool = os.getenv("START_COMMAND_QUEUE_WORKER", "true").lower() == "true"
    START_RETENTION_WORKER: bool = os.getenv("START_RETENTION_WORKER", "true").lower() == "true"
    DEVICE_STATUS_RETENTION_DAYS: int = int(os.getenv("DEVICE_STATUS_RETENTION_DAYS", "60"))
    MQTT_COMMAND_QOS: int = int(os.getenv("MQTT_COMMAND_QOS", "1"))
    OTA_FIRMWARE_URL: str = os.getenv("OTA_FIRMWARE_URL", "")
    FIRMWARE_UPLOAD_DIR: str = os.getenv("FIRMWARE_UPLOAD_DIR", "firmware_uploads")
    BACKEND_PUBLIC_URL: str = os.getenv("BACKEND_PUBLIC_URL", "")
    MP_ACCESS_TOKEN: str = os.getenv("MP_ACCESS_TOKEN", "")
    MP_WEBHOOK_SECRET: str = os.getenv("MP_WEBHOOK_SECRET", "")
    MP_APP_ID: str = os.getenv("MP_APP_ID", os.getenv("MP_CLIENT_ID", ""))
    MP_CLIENT_SECRET: str = os.getenv("MP_CLIENT_SECRET", "")
    MP_OAUTH_REDIRECT_URI: str = os.getenv("MP_OAUTH_REDIRECT_URI", "")
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:5173")
    CORS_ALLOWED_ORIGINS: str = os.getenv("CORS_ALLOWED_ORIGINS", "")
    MP_DEFAULT_STORE_STREET_NAME: str = os.getenv("MP_DEFAULT_STORE_STREET_NAME", "Rua CompactPay")
    MP_DEFAULT_STORE_STREET_NUMBER: str = os.getenv("MP_DEFAULT_STORE_STREET_NUMBER", "0")
    MP_DEFAULT_STORE_CITY_NAME: str = os.getenv("MP_DEFAULT_STORE_CITY_NAME", "Sao Paulo")
    MP_DEFAULT_STORE_STATE_NAME: str = os.getenv("MP_DEFAULT_STORE_STATE_NAME", "Sao Paulo")
    MP_DEFAULT_STORE_LATITUDE: float = float(os.getenv("MP_DEFAULT_STORE_LATITUDE", "-23.55052"))
    MP_DEFAULT_STORE_LONGITUDE: float = float(os.getenv("MP_DEFAULT_STORE_LONGITUDE", "-46.633308"))
    MP_DEFAULT_POS_CATEGORY: int = int(os.getenv("MP_DEFAULT_POS_CATEGORY", "7994"))
    MP_POS_CATEGORY_FALLBACKS: str = os.getenv("MP_POS_CATEGORY_FALLBACKS", "7994,7996,7999,5999,5399")
    START_ALERT_NOTIFIER_WORKER: bool = os.getenv("START_ALERT_NOTIFIER_WORKER", "true").lower() == "true"
    ALERT_NOTIFIER_INTERVAL_SECONDS: int = int(os.getenv("ALERT_NOTIFIER_INTERVAL_SECONDS", "120"))
    ALERT_RENOTIFY_COOLDOWN_MINUTES: int = int(os.getenv("ALERT_RENOTIFY_COOLDOWN_MINUTES", "60"))
    ALERT_NOTIFY_SEVERIDADES: str = os.getenv("ALERT_NOTIFY_SEVERIDADES", "critico")
    ALERT_NOTIFICATION_EMAILS: str = os.getenv("ALERT_NOTIFICATION_EMAILS", "")
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USERNAME: str = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_USE_TLS: bool = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
    SMTP_FROM_EMAIL: str = os.getenv("SMTP_FROM_EMAIL", "")


settings = Settings()


def mqtt_tls_kwargs() -> dict | None:
    if not settings.MQTT_USE_TLS:
        return None
    return {
        "ca_certs": settings.AWS_CA_PATH,
        "certfile": settings.AWS_CERT_PATH,
        "keyfile": settings.AWS_KEY_PATH,
        "cert_reqs": ssl.CERT_REQUIRED,
        "tls_version": ssl.PROTOCOL_TLSv1_2,
    }

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db"
os.environ["START_MQTT_WORKER"] = "false"
os.environ["START_COMMAND_QUEUE_WORKER"] = "false"
os.environ["START_RETENTION_WORKER"] = "false"
os.environ["START_ALERT_NOTIFIER_WORKER"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import AlertaNotificacao, Maquina
import app.models.produto  # noqa: F401
from app.services import alert_notifier

Base.metadata.create_all(bind=engine)


def _create_maquina(id_hardware, **kwargs):
    db = SessionLocal()
    try:
        maquina = db.query(Maquina).filter(Maquina.id_hardware == id_hardware).first()
        if not maquina:
            maquina = Maquina(id_hardware=id_hardware, nome_local=id_hardware)
            db.add(maquina)
        for key, value in kwargs.items():
            setattr(maquina, key, value)
        db.commit()
    finally:
        db.close()


def _get_notificacao(alerta_key):
    db = SessionLocal()
    try:
        return db.query(AlertaNotificacao).filter(AlertaNotificacao.alerta_key == alerta_key).first()
    finally:
        db.close()


def _configure_smtp(monkeypatch_holder):
    monkeypatch_holder["smtp_host"] = settings.SMTP_HOST
    monkeypatch_holder["emails"] = settings.ALERT_NOTIFICATION_EMAILS
    settings.SMTP_HOST = "smtp.test.local"
    settings.ALERT_NOTIFICATION_EMAILS = "ops@test.local"


def _restore_smtp(monkeypatch_holder):
    settings.SMTP_HOST = monkeypatch_holder["smtp_host"]
    settings.ALERT_NOTIFICATION_EMAILS = monkeypatch_holder["emails"]


def test_send_email_is_noop_without_smtp_configuration():
    with patch("app.services.alert_notifier.smtplib.SMTP") as smtp_mock:
        alert_notifier._send_email("assunto", "corpo")

    smtp_mock.assert_not_called()


def test_send_email_sends_via_smtp_when_configured():
    holder = {}
    _configure_smtp(holder)
    try:
        smtp_instance = MagicMock()
        smtp_instance.__enter__.return_value = smtp_instance
        with patch("app.services.alert_notifier.smtplib.SMTP", return_value=smtp_instance) as smtp_mock:
            alert_notifier._send_email("assunto de teste", "corpo de teste")

        smtp_mock.assert_called_once()
        smtp_instance.starttls.assert_called_once()
        smtp_instance.sendmail.assert_called_once()
        args = smtp_instance.sendmail.call_args.args
        assert args[1] == ["ops@test.local"]
        # O corpo vai codificado em base64 pelo MIMEText - basta confirmar que o
        # cabecalho (nao codificado) chegou certo ate o SMTP.
        assert "Subject: assunto de teste" in args[2]
    finally:
        _restore_smtp(holder)


def test_check_and_notify_creates_row_and_sends_email_for_new_critical_alert():
    holder = {}
    _configure_smtp(holder)
    machine_id = "CPM-NOTIFY-OFFLINE"
    _create_maquina(machine_id, ultimo_sinal=datetime.utcnow() - timedelta(minutes=10))
    try:
        with patch("app.services.alert_notifier._send_email") as send_mock:
            alert_notifier.check_and_notify_alerts()

        send_mock.assert_called_once()
        subject = send_mock.call_args.args[0]
        assert "Novo alerta" in subject

        row = _get_notificacao(f"{machine_id}:offline")
        assert row is not None
        assert row.resolvido_em is None
    finally:
        _restore_smtp(holder)


def test_check_and_notify_does_not_renotify_within_cooldown():
    holder = {}
    _configure_smtp(holder)
    machine_id = "CPM-NOTIFY-COOLDOWN"
    _create_maquina(machine_id, ultimo_sinal=datetime.utcnow() - timedelta(minutes=10))
    try:
        with patch("app.services.alert_notifier._send_email") as send_mock:
            alert_notifier.check_and_notify_alerts()
            alert_notifier.check_and_notify_alerts()

        assert send_mock.call_count == 1
    finally:
        _restore_smtp(holder)


def test_check_and_notify_renotifies_after_cooldown_expires():
    holder = {}
    _configure_smtp(holder)
    machine_id = "CPM-NOTIFY-RENOTIFY"
    _create_maquina(machine_id, ultimo_sinal=datetime.utcnow() - timedelta(minutes=10))
    try:
        with patch("app.services.alert_notifier._send_email"):
            alert_notifier.check_and_notify_alerts()

        db = SessionLocal()
        try:
            row = db.query(AlertaNotificacao).filter(AlertaNotificacao.alerta_key == f"{machine_id}:offline").first()
            row.ultima_notificacao_em = datetime.utcnow() - timedelta(
                minutes=settings.ALERT_RENOTIFY_COOLDOWN_MINUTES + 5
            )
            db.commit()
        finally:
            db.close()

        with patch("app.services.alert_notifier._send_email") as send_mock:
            alert_notifier.check_and_notify_alerts()

        send_mock.assert_called_once()
        assert "ainda ativo" in send_mock.call_args.args[0]
    finally:
        _restore_smtp(holder)


def test_check_and_notify_marks_resolved_and_sends_resolution_email():
    holder = {}
    _configure_smtp(holder)
    machine_id = "CPM-NOTIFY-RESOLVED"
    _create_maquina(machine_id, ultimo_sinal=datetime.utcnow() - timedelta(minutes=10))
    try:
        with patch("app.services.alert_notifier._send_email"):
            alert_notifier.check_and_notify_alerts()

        _create_maquina(machine_id, ultimo_sinal=datetime.utcnow())

        with patch("app.services.alert_notifier._send_email") as send_mock:
            alert_notifier.check_and_notify_alerts()

        row = _get_notificacao(f"{machine_id}:offline")
        assert row.resolvido_em is not None
        send_mock.assert_called_once()
        assert "Resolvido" in send_mock.call_args.args[0]
    finally:
        _restore_smtp(holder)


def test_check_and_notify_ignores_non_critical_severities_by_default():
    holder = {}
    _configure_smtp(holder)
    machine_id = "CPM-NOTIFY-WIFI"
    _create_maquina(machine_id, ultimo_sinal=datetime.utcnow(), wifi_quality=5)
    try:
        with patch("app.services.alert_notifier._send_email") as send_mock:
            alert_notifier.check_and_notify_alerts()

        send_mock.assert_not_called()
        assert _get_notificacao(f"{machine_id}:wifi_ruim") is None
    finally:
        _restore_smtp(holder)

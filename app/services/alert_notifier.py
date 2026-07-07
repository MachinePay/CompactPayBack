import logging
import smtplib
import threading
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText

from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.models import AlertaNotificacao, Maquina
from app.services.maquinas_relatorio import compute_active_alerts


def _severidades_notificaveis() -> set[str]:
    raw = settings.ALERT_NOTIFY_SEVERIDADES or "critico"
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _destinatarios() -> list[str]:
    raw = settings.ALERT_NOTIFICATION_EMAILS or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _send_email(subject: str, body: str) -> None:
    destinatarios = _destinatarios()
    if not destinatarios or not settings.SMTP_HOST:
        logging.warning("Notificacao de alerta nao enviada: SMTP_HOST ou ALERT_NOTIFICATION_EMAILS nao configurados")
        return

    remetente = settings.SMTP_FROM_EMAIL or settings.SMTP_USERNAME
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = remetente
    msg["To"] = ", ".join(destinatarios)

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as server:
        if settings.SMTP_USE_TLS:
            server.starttls()
        if settings.SMTP_USERNAME:
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        server.sendmail(remetente, destinatarios, msg.as_string())


def _format_alert_email(alert: dict, evento: str) -> tuple[str, str]:
    machine = alert["maquina"]
    nome = machine.get("nome") or machine.get("id_hardware")
    subject = f"[CompactPay] {evento}: {alert['titulo']} - {nome}"
    body = (
        f"Maquina: {nome} ({machine.get('id_hardware')})\n"
        f"Cliente: {machine.get('cliente_nome') or 'Sem cliente'}\n"
        f"Localizacao: {machine.get('localizacao') or '--'}\n"
        f"Severidade: {alert['severidade']}\n"
        f"Alerta: {alert['titulo']}\n"
        f"Detalhes: {alert['mensagem']}\n"
    )
    return subject, body


def check_and_notify_alerts() -> None:
    severidades = _severidades_notificaveis()
    cooldown = timedelta(minutes=settings.ALERT_RENOTIFY_COOLDOWN_MINUTES)
    now = datetime.utcnow()

    db = SessionLocal()
    try:
        maquinas = db.query(Maquina).options(joinedload(Maquina.dono)).all()
        id_para_nome = {maquina.id_hardware: (maquina.nome_local or maquina.id_hardware) for maquina in maquinas}
        alerts = compute_active_alerts(db, maquinas, now)
        notificaveis = {alert["id"]: alert for alert in alerts if alert["severidade"] in severidades}

        existentes = {
            row.alerta_key: row
            for row in db.query(AlertaNotificacao).filter(AlertaNotificacao.resolvido_em.is_(None)).all()
        }

        for alerta_key, alert in notificaveis.items():
            existente = existentes.get(alerta_key)
            if existente is None:
                subject, body = _format_alert_email(alert, "Novo alerta")
                _send_email(subject, body)
                db.add(
                    AlertaNotificacao(
                        alerta_key=alerta_key,
                        maquina_id=alert["maquina"]["id_hardware"],
                        tipo=alert["tipo"],
                        severidade=alert["severidade"],
                        primeira_notificacao_em=now,
                        ultima_notificacao_em=now,
                    )
                )
            elif now - existente.ultima_notificacao_em >= cooldown:
                subject, body = _format_alert_email(alert, "Alerta ainda ativo")
                _send_email(subject, body)
                existente.ultima_notificacao_em = now

        for alerta_key, existente in existentes.items():
            if alerta_key not in notificaveis:
                existente.resolvido_em = now
                nome = id_para_nome.get(existente.maquina_id, existente.maquina_id)
                subject = f"[CompactPay] Resolvido: {existente.tipo} - {nome}"
                body = f"O alerta '{existente.tipo}' da maquina {nome} ({existente.maquina_id}) nao esta mais ativo."
                _send_email(subject, body)

        db.commit()
    except Exception:
        logging.exception("Erro ao verificar/enviar notificacoes de alerta")
        db.rollback()
    finally:
        db.close()


def run_alert_notifier_worker() -> None:
    logging.info("Alert notifier worker iniciado")
    while True:
        try:
            check_and_notify_alerts()
        except Exception:
            logging.exception("Erro no alert notifier worker")
        time.sleep(settings.ALERT_NOTIFIER_INTERVAL_SECONDS)


def start_alert_notifier_worker() -> threading.Thread | None:
    if not settings.START_ALERT_NOTIFIER_WORKER:
        logging.info("Alert notifier worker desativado por START_ALERT_NOTIFIER_WORKER=false")
        return None
    if not settings.SMTP_HOST or not _destinatarios():
        logging.info(
            "Alert notifier worker desativado: configure SMTP_HOST e ALERT_NOTIFICATION_EMAILS para ativar"
        )
        return None
    thread = threading.Thread(target=run_alert_notifier_worker, daemon=True)
    thread.start()
    return thread

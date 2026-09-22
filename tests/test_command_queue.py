import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db"
os.environ["START_MQTT_WORKER"] = "false"
os.environ["START_COMMAND_QUEUE_WORKER"] = "false"
os.environ["START_RETENTION_WORKER"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.models import Cliente, ComandoMaquina, HistoricoOperacao, Maquina, VendaPagamento
import app.models.models  # noqa: F401
import app.models.produto  # noqa: F401
from app.services.command_queue import (
    MAX_ATTEMPTS,
    process_due_command_retries,
    track_and_publish_command,
    update_command_from_device_status,
    update_command_from_pulse_status,
)

Base.metadata.create_all(bind=engine)


def _get_comando(command_id):
    db = SessionLocal()
    try:
        return db.query(ComandoMaquina).filter(ComandoMaquina.command_id == command_id).first()
    finally:
        db.close()


def test_track_and_publish_command_success_marks_enviado():
    with patch("app.services.mqtt_commands.publish_raw_mqtt_command") as publish_mock:
        track_and_publish_command(
            machine_id="CPM-QUEUE-1",
            command_id="cmd-success-1",
            tipo="paid",
            topic="/TEF/CPM-QUEUE-1/cmd",
            payload="CPM-QUEUE-1@paid|cmd=cmd-success-1|",
        )

    publish_mock.assert_called_once_with("/TEF/CPM-QUEUE-1/cmd", "CPM-QUEUE-1@paid|cmd=cmd-success-1|")
    comando = _get_comando("cmd-success-1")
    assert comando is not None
    assert comando.status == "enviado"
    assert comando.tentativas == 1
    assert comando.sent_at is not None
    assert comando.next_retry_at is not None
    assert comando.ultimo_erro is None


def test_track_and_publish_command_without_command_id_skips_persistence():
    with patch("app.services.mqtt_commands.publish_raw_mqtt_command") as publish_mock:
        track_and_publish_command(
            machine_id="CPM-QUEUE-2",
            command_id=None,
            tipo="ping",
            topic="/TEF/CPM-QUEUE-2/cmd",
            payload="CPM-QUEUE-2@ping|",
        )

    publish_mock.assert_called_once_with("/TEF/CPM-QUEUE-2/cmd", "CPM-QUEUE-2@ping|")
    db = SessionLocal()
    try:
        assert db.query(ComandoMaquina).filter(ComandoMaquina.maquina_id == "CPM-QUEUE-2").count() == 0
    finally:
        db.close()


def test_track_and_publish_command_failure_marks_aguardando_retry_and_raises():
    with patch("app.services.mqtt_commands.publish_raw_mqtt_command", side_effect=RuntimeError("broker off")):
        try:
            track_and_publish_command(
                machine_id="CPM-QUEUE-3",
                command_id="cmd-fail-1",
                tipo="paid",
                topic="/TEF/CPM-QUEUE-3/cmd",
                payload="CPM-QUEUE-3@paid|cmd=cmd-fail-1|",
            )
            assert False, "esperava que a excecao de publicacao fosse propagada"
        except RuntimeError:
            pass

    comando = _get_comando("cmd-fail-1")
    assert comando is not None
    assert comando.tentativas == 1
    assert comando.status == "aguardando_retry"
    assert comando.ultimo_erro == "broker off"
    assert comando.next_retry_at is not None


def test_process_due_command_retries_resends_and_updates_status():
    db = SessionLocal()
    try:
        comando = ComandoMaquina(
            command_id="cmd-retry-ok",
            maquina_id="CPM-QUEUE-4",
            tipo="paid",
            topic="/TEF/CPM-QUEUE-4/cmd",
            payload="CPM-QUEUE-4@paid|cmd=cmd-retry-ok|",
            status="aguardando_retry",
            tentativas=1,
            max_tentativas=MAX_ATTEMPTS,
            next_retry_at=datetime.utcnow() - timedelta(seconds=1),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(comando)
        db.commit()
    finally:
        db.close()

    with patch("app.services.mqtt_commands.publish_raw_mqtt_command") as publish_mock:
        processed = process_due_command_retries()

    publish_mock.assert_called_once()
    assert processed == 1
    comando = _get_comando("cmd-retry-ok")
    assert comando.status == "enviado"
    assert comando.tentativas == 2


def test_process_due_command_retries_marks_falhou_after_attempts_exhausted():
    db = SessionLocal()
    try:
        comando = ComandoMaquina(
            command_id="cmd-retry-exhausted",
            maquina_id="CPM-QUEUE-5",
            tipo="paid",
            topic="/TEF/CPM-QUEUE-5/cmd",
            payload="CPM-QUEUE-5@paid|cmd=cmd-retry-exhausted|",
            status="falha_publicacao",
            tentativas=MAX_ATTEMPTS,
            max_tentativas=MAX_ATTEMPTS,
            next_retry_at=datetime.utcnow() - timedelta(seconds=1),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(comando)
        db.commit()
    finally:
        db.close()

    with patch("app.services.mqtt_commands.publish_raw_mqtt_command") as publish_mock:
        processed = process_due_command_retries()

    publish_mock.assert_not_called()
    assert processed == 0
    comando = _get_comando("cmd-retry-exhausted")
    assert comando.status == "falhou"
    assert comando.detalhe_status == "retry_esgotado"
    assert comando.next_retry_at is None
    assert comando.finished_at is not None


def test_update_command_from_device_status_tracks_ack_and_final_state():
    with patch("app.services.mqtt_commands.publish_raw_mqtt_command"):
        track_and_publish_command(
            machine_id="CPM-QUEUE-6",
            command_id="cmd-lifecycle",
            tipo="paid",
            topic="/TEF/CPM-QUEUE-6/cmd",
            payload="CPM-QUEUE-6@paid|cmd=cmd-lifecycle|",
        )

    update_command_from_device_status("cmd-lifecycle", "CMD_RECEBIDO")
    comando = _get_comando("cmd-lifecycle")
    assert comando.status == "recebido"
    assert comando.ack_at is not None

    update_command_from_pulse_status("cmd-lifecycle", "pulso_confirmado")
    comando = _get_comando("cmd-lifecycle")
    assert comando.status == "executado"
    assert comando.finished_at is not None
    assert comando.next_retry_at is None


def test_pulso_nao_confirmado_does_not_finalize_command_as_falhou():
    # Maquina sem o fio do contador ligado manda PULSO_NAO_CONFIRMADO pra
    # CADA pulso da sequencia (nenhum confirma na volta), mas sempre termina
    # mandando o status agregado real. PULSO_NAO_CONFIRMADO sozinho nao pode
    # finalizar o comando como falha - senao quem estiver acompanhando via
    # polling (GET /comandos-maquinas) pode flagrar "falhou" bem no meio do
    # caminho, antes do status final (de sucesso) chegar.
    with patch("app.services.mqtt_commands.publish_raw_mqtt_command"):
        track_and_publish_command(
            machine_id="CPM-QUEUE-8",
            command_id="cmd-sem-contador",
            tipo="paid",
            topic="/TEF/CPM-QUEUE-8/cmd",
            payload="CPM-QUEUE-8@paid|cmd=cmd-sem-contador|",
        )

    update_command_from_device_status("cmd-sem-contador", "CMD_RECEBIDO")
    update_command_from_device_status("cmd-sem-contador", "PULSO_INICIADO")

    # Pulsos 1 e 2 nao confirmam (sem fio do contador) - nao pode virar "falhou".
    update_command_from_device_status("cmd-sem-contador", "PULSO_NAO_CONFIRMADO")
    comando = _get_comando("cmd-sem-contador")
    assert comando.status == "executando"

    update_command_from_device_status("cmd-sem-contador", "PULSO_NAO_CONFIRMADO")
    comando = _get_comando("cmd-sem-contador")
    assert comando.status == "executando"

    # Status agregado final da placa: enviou tudo, so nao confirmou o retorno.
    update_command_from_device_status("cmd-sem-contador", "PULSOS_ENVIADOS_SEM_RETORNO")
    comando = _get_comando("cmd-sem-contador")
    assert comando.status == "executado"
    assert comando.detalhe_status == "PULSOS_ENVIADOS_SEM_RETORNO"


def test_final_status_is_not_downgraded_by_late_events():
    with patch("app.services.mqtt_commands.publish_raw_mqtt_command"):
        track_and_publish_command(
            machine_id="CPM-QUEUE-7",
            command_id="cmd-final-guard",
            tipo="paid",
            topic="/TEF/CPM-QUEUE-7/cmd",
            payload="CPM-QUEUE-7@paid|cmd=cmd-final-guard|",
        )

    update_command_from_device_status("cmd-final-guard", "PULSOS_CONCLUIDOS")
    comando = _get_comando("cmd-final-guard")
    assert comando.status == "executado"

    # Um evento tardio (ex.: CMD_RECEBIDO duplicado chegando fora de ordem) nao pode
    # reabrir um comando que ja foi confirmado como executado.
    update_command_from_device_status("cmd-final-guard", "CMD_RECEBIDO")
    comando = _get_comando("cmd-final-guard")
    assert comando.status == "executado"


def test_credit_command_with_zero_response_marks_pulse_offline_and_auto_refunds():
    # Placa nunca manda nem um CMD_RECEBIDO (ack_at continua None) depois de
    # esgotar as tentativas de reenvio - diferente de "recebeu e nao
    # confirmou o pulso final", aqui da pra ter certeza que o pulso fisico
    # nunca aconteceu, entao o estorno automatico tem que disparar sozinho.
    db = SessionLocal()
    try:
        cliente = Cliente(
            nome_empresa="Cliente Teste Offline",
            email_contato="offline@teste.com",
            api_key="api-key-offline-teste",
            mp_access_token="TOKEN-TESTE-OFFLINE",
        )
        db.add(cliente)
        db.flush()

        maquina = Maquina(id_hardware="CPM-QUEUE-OFFLINE", cliente_id=cliente.id, nome_local="Maquina Offline")
        db.add(maquina)
        db.flush()

        historico = HistoricoOperacao(
            maquina_id=maquina.id_hardware,
            categoria="PAGAMENTO",
            descricao="Pagamento maquininha aprovado (payment_id=pay-offline-1)",
            valor=5.0,
            provider="mercado_pago",
            provider_payment_id="pay-offline-1",
            pulse_status="pulso_iniciado",
            command_id="cmd-offline-1",
        )
        db.add(historico)
        db.flush()

        venda = VendaPagamento(
            maquina_id=maquina.id_hardware,
            historico_id=historico.id,
            origem="pix",
            provider="mercado_pago",
            provider_payment_id="pay-offline-1",
            valor_bruto=5.0,
            valor_liquido=5.0,
            status_pulso="pulso_iniciado",
            command_id="cmd-offline-1",
        )
        db.add(venda)

        comando = ComandoMaquina(
            command_id="cmd-offline-1",
            maquina_id=maquina.id_hardware,
            tipo="paid",
            topic="/TEF/CPM-QUEUE-OFFLINE/cmd",
            payload="CPM-QUEUE-OFFLINE@paid|cmd=cmd-offline-1|",
            status="aguardando_retry",
            tentativas=MAX_ATTEMPTS,
            max_tentativas=MAX_ATTEMPTS,
            ack_at=None,
            next_retry_at=datetime.utcnow() - timedelta(seconds=1),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(comando)
        db.commit()
    finally:
        db.close()

    with patch("app.services.mqtt_commands.publish_raw_mqtt_command") as publish_mock, patch(
        "app.services.pagamentos_helpers.mp_request"
    ) as mp_request_mock:
        process_due_command_retries()

    publish_mock.assert_not_called()
    assert mp_request_mock.call_count == 1
    called_url = mp_request_mock.call_args.args[1]
    assert "pay-offline-1/refunds" in called_url

    comando = _get_comando("cmd-offline-1")
    assert comando.status == "falhou"
    assert comando.detalhe_status == "retry_esgotado"

    db = SessionLocal()
    try:
        historico = (
            db.query(HistoricoOperacao)
            .filter(HistoricoOperacao.command_id == "cmd-offline-1")
            .first()
        )
        venda = db.query(VendaPagamento).filter(VendaPagamento.command_id == "cmd-offline-1").first()
        assert historico.pulse_status == "falha_dispositivo_offline"
        assert historico.refunded_at is not None
        assert venda.status_pulso == "falha_dispositivo_offline"
        assert venda.refunded_at is not None
    finally:
        db.close()

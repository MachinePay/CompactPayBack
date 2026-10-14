"""Rede/senha de Wi-Fi de cada maquina (historico, troca remota pelo painel).

A placa manda STATUS|WIFI_INFO|ssid_hex=..|pass_hex=.. so quando a rede muda
(firmware 2.8.0+). A senha fica criptografada no banco (Fernet) e o historico
tem uma linha por combinacao rede+senha: repetir a mesma so atualiza
"ultimo_visto_em". Nada disso vai para historico_operacoes nem para os logs.

Chave: WIFI_CRYPT_KEY (variavel de ambiente) se existir; senao derivada do
SECRET_KEY. Se a chave mudar, senhas antigas ficam ilegiveis (o painel mostra
isso) e a placa pode reenviar a atual pelo botao "Pedir rede atual".
"""
import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken

from app.core.security import SECRET_KEY
from app.models.models import Maquina, MaquinaWifiCredencial, MaquinaWifiTentativa

SSID_MAX = 32
SENHA_MIN = 8
SENHA_MAX = 63


def _chave_base() -> bytes:
    return (os.getenv("WIFI_CRYPT_KEY") or f"wifi:{SECRET_KEY}").encode("utf-8")


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(_chave_base()).digest()))


def criptografar_senha(senha: str) -> str:
    return _fernet().encrypt(senha.encode("utf-8")).decode("ascii")


def descriptografar_senha(senha_cript: str | None) -> str | None:
    """None se nao der para ler (chave mudou ou dado corrompido)."""
    if not senha_cript:
        return ""
    try:
        return _fernet().decrypt(senha_cript.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def hash_credencial(ssid: str, senha: str) -> str:
    """Identifica a combinacao rede+senha sem guardar a senha em claro."""
    return hmac.new(_chave_base(), f"{ssid}\0{senha}".encode("utf-8"), hashlib.sha256).hexdigest()


def decodificar_hex(valor: str | None, limite: int) -> str | None:
    """Texto que a placa manda em hex (evita problema com "|" no SSID/senha)."""
    try:
        bruto = bytes.fromhex((valor or "").strip())
    except ValueError:
        return None
    if len(bruto) > limite or b"\x00" in bruto:
        return None
    return bruto.decode("utf-8", errors="replace")


def validar_rede(ssid: str, senha: str) -> str | None:
    """Mensagem de erro para o painel, ou None se estiver ok."""
    if not ssid or not ssid.strip():
        return "Informe o nome da rede Wi-Fi"
    if len(ssid.encode("utf-8")) > SSID_MAX:
        return f"O nome da rede pode ter no maximo {SSID_MAX} caracteres"
    tamanho = len(senha.encode("utf-8"))
    if tamanho and (tamanho < SENHA_MIN or tamanho > SENHA_MAX):
        return f"A senha precisa ter entre {SENHA_MIN} e {SENHA_MAX} caracteres (ou ficar vazia em rede aberta)"
    if "\x00" in ssid or "\x00" in senha:
        return "Caractere invalido no nome ou na senha"
    return None


def registrar_wifi_info(db, maquina: Maquina, ssid: str, senha: str) -> MaquinaWifiCredencial:
    """Guarda a rede atual da maquina. Mesma rede+senha ja vista so atualiza a
    data; combinacao nova vira uma linha nova no historico."""
    agora = datetime.utcnow()
    chave = hash_credencial(ssid, senha)
    registro = (
        db.query(MaquinaWifiCredencial)
        .filter(
            MaquinaWifiCredencial.maquina_id == maquina.id_hardware,
            MaquinaWifiCredencial.credencial_hash == chave,
        )
        .first()
    )
    if registro:
        registro.ultimo_visto_em = agora
    else:
        registro = MaquinaWifiCredencial(
            maquina_id=maquina.id_hardware,
            ssid=ssid,
            senha_cript=criptografar_senha(senha) if senha else "",
            credencial_hash=chave,
            primeiro_visto_em=agora,
            ultimo_visto_em=agora,
        )
        db.add(registro)
    maquina.wifi_ssid = ssid
    maquina.wifi_credencial_hash = chave
    _confirmar_tentativa_pela_rede(db, maquina.id_hardware, ssid, agora)
    return registro


def _confirmar_tentativa_pela_rede(db, machine_id: str, ssid: str, agora: datetime) -> None:
    pendentes = (
        db.query(MaquinaWifiTentativa)
        .filter(
            MaquinaWifiTentativa.maquina_id == machine_id,
            MaquinaWifiTentativa.resultado.in_(["testando", "sem_confirmacao"]),
            MaquinaWifiTentativa.ssid == ssid,
            MaquinaWifiTentativa.created_at >= agora - TENTATIVA_CONFIRMADA_POR_INFO_JANELA,
        )
        .all()
    )
    for tentativa in pendentes:
        tentativa.resultado = "trocado"
        tentativa.motivo = None
        tentativa.finished_at = agora


def fechar_tentativas_sem_confirmacao(db, machine_id: str | None = None) -> None:
    """Marca como "sem_confirmacao" as trocas paradas em "testando"."""
    limite = datetime.utcnow() - TENTATIVA_SEM_CONFIRMACAO_APOS
    query = db.query(MaquinaWifiTentativa).filter(
        MaquinaWifiTentativa.resultado == "testando", MaquinaWifiTentativa.created_at <= limite
    )
    if machine_id:
        query = query.filter(MaquinaWifiTentativa.maquina_id == machine_id)
    for tentativa in query.all():
        tentativa.resultado = "sem_confirmacao"
        tentativa.motivo = "sem_confirmacao"
        tentativa.finished_at = datetime.utcnow()


def interpretar_redes(texto: str | None) -> list[dict]:
    """Lista que a placa manda em WIFI_REDES: "ssid_hex,rssi,seguranca;..."."""
    redes = []
    for item in (texto or "").split(";"):
        partes = item.split(",")
        if len(partes) != 3:
            continue
        ssid = decodificar_hex(partes[0], SSID_MAX)
        try:
            rssi = int(partes[1])
            seguranca = int(partes[2])
        except ValueError:
            continue
        if ssid:
            redes.append({"ssid": ssid, "rssi": rssi, "aberta": seguranca == 0, "wep": seguranca == 1})
    return redes


def motivo_por_codigo_wifi(codigo: int) -> str:
    """Mesmo mapeamento do firmware (executarTrocaWiFi) para o motivo da
    queda do Wi-Fi (wifi_err_reason_t)."""
    if codigo in (2, 15, 23, 202, 204):
        return "senha_errada"
    if codigo in (201, 210, 211, 212):
        return "rede_nao_encontrada"
    return f"wifi_nao_conectou_{codigo}"


def registrar_tentativa_portal(db, machine_id: str, fields: dict[str, str]) -> MaquinaWifiTentativa | None:
    """STATUS|WIFI_PORTAL_TENTATIVA: rede/senha digitadas no portal do
    celular, mandadas quando a placa conseguiu conectar. r=1 conectou,
    r=0 sem resultado, outro = codigo da queda do Wi-Fi."""
    import uuid

    ssid = decodificar_hex(fields.get("ssid_hex"), SSID_MAX)
    senha = decodificar_hex(fields.get("pass_hex"), 64)
    try:
        codigo = int(fields.get("r") or 0)
    except ValueError:
        codigo = 0
    if not ssid or senha is None:
        return None
    if codigo == 1:
        resultado, motivo = "trocado", None
    elif codigo == 0:
        resultado, motivo = "falhou", "sem_resultado"
    else:
        resultado, motivo = "falhou", motivo_por_codigo_wifi(codigo)
    agora = datetime.utcnow()
    tentativa = MaquinaWifiTentativa(
        maquina_id=machine_id,
        command_id=f"portal-{uuid.uuid4()}",
        origem="portal",
        ssid=ssid,
        senha_cript=criptografar_senha(senha) if senha else "",
        resultado=resultado,
        motivo=motivo,
        solicitado_por="Portal (celular)",
        created_at=agora,
        finished_at=agora,
    )
    db.add(tentativa)
    return tentativa


RESULTADO_POR_STATUS = {
    "WIFI_TROCADO": "trocado",
    "WIFI_TROCA_FALHOU": "falhou",
    "WIFI_TROCA_RECUSADA": "recusado",
}

MOTIVOS_LEGIVEIS = {
    "senha_errada": "Senha errada",
    "rede_nao_encontrada": "Rede nao encontrada perto da maquina",
    "rede_exige_senha": "A rede exige senha",
    "rede_aberta_deixe_senha_vazia": "A rede e aberta: deixe a senha vazia",
    "rede_wep_nao_suportada": "Rede com seguranca WEP (antiga) nao e suportada",
    "senha_tamanho_invalido": "A senha precisa ter entre 8 e 63 caracteres",
    "maquina_ocupada": "A maquina estava liberando credito ou atualizando - tente de novo",
    "sem_acesso_ao_servidor": "Conectou no Wi-Fi, mas a rede nao tem internet ou bloqueia o servidor",
    "varredura_falhou": "A placa nao conseguiu procurar redes - tente de novo",
    "dados_invalidos": "Dados invalidos",
    "sem_resposta": "A placa nao respondeu",
    "sem_resultado": "Nao conectou (o portal foi fechado ou expirou antes de outra tentativa)",
    "sem_confirmacao": "A placa nao confirmou o resultado - use 'Pedir rede atual' para ver em qual rede ela ficou",
}

# Troca sem resultado depois disso vira "sem_confirmacao" (o teste completo
# leva no maximo ~2 min na placa).
TENTATIVA_SEM_CONFIRMACAO_APOS = timedelta(minutes=5)
# WIFI_INFO com a rede pedida dentro dessa janela confirma a troca, mesmo se o
# WIFI_TROCADO se perdeu.
TENTATIVA_CONFIRMADA_POR_INFO_JANELA = timedelta(minutes=15)


def motivo_legivel(motivo: str | None) -> str | None:
    if not motivo:
        return None
    if motivo.startswith("wifi_nao_conectou_"):
        return f"Nao conectou no Wi-Fi (codigo {motivo.rsplit('_', 1)[-1]})"
    return MOTIVOS_LEGIVEIS.get(motivo, motivo)


def atualizar_tentativa(db, command_id: str | None, status: str, fields: dict[str, str]) -> None:
    """Fecha a tentativa de troca quando a placa responde o resultado."""
    resultado = RESULTADO_POR_STATUS.get(status)
    if not command_id or not resultado:
        return
    tentativa = db.query(MaquinaWifiTentativa).filter(MaquinaWifiTentativa.command_id == command_id).first()
    if not tentativa or tentativa.resultado not in ("testando", "sem_confirmacao"):
        return
    tentativa.resultado = resultado
    tentativa.motivo = fields.get("motivo") or None
    tentativa.finished_at = datetime.utcnow()

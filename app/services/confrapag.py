"""Banco "confrapag_sdk": app CompactPay na maquininha PAX com o SDK ConfraPag
(android-confrapag/). So SDK: cartao de credito/debito no proprio terminal.

Fluxo: o app cobra pelo SDK e manda a venda aprovada para POST /confrapag/pagamentos.

Diferente do Mercado Pago, a ConfraPag NAO tem API para o backend reconsultar a
venda do terminal (o SDK nao tem token; o terminal fala direto com o TKPP). Entao
a confianca aqui e' a chave do terminal, e o backend limita o que um terminal
pode pedir:
- so maquinas confrapag_sdk do mesmo cliente da chave;
- so valores cadastrados como produto da maquina (ou os padroes, se nao houver);
- referencia CP-{placa}-{timestamp} desta placa, gerada ha menos de 2 h;
- cada TID libera credito uma vez so (retentativa recebe o mesmo comando).

Nada aqui altera webhooks, rotas ou fluxos que ja existem.
"""
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.confrapag import CobrancaPixConfrapag, TerminalConfrapag
from app.models.models import Cliente, EventoTipo, HistoricoOperacao, Maquina, MetodoPagamento, Transacao, VendaPagamento
from app.models.produto import Produto
from app.services import confrapix
from app.services.command_queue import get_command_status
from app.services.mercado_pago_webhook import _acquire_payment_lock
from app.services.mqtt_commands import publish_machine_credit_pulses
from app.services.pagamentos_helpers import calcular_pulsos_por_valor
from app.services.pulse_tracking import update_pulse_status
from app.services.smartapp import gerar_chave, hash_chave, maquina_online, verificar_placa_online  # noqa: F401
from app.services.vendas import registrar_venda_pagamento

BANCO_CONFRAPAG = "confrapag_sdk"
PROVIDER = "confrapag"
REFERENCIA_IDADE_MAXIMA = timedelta(hours=2)
REFERENCIA_FUTURO_TOLERANCIA = timedelta(minutes=10)  # relogio do terminal adiantado
# Mesmos valores que o app da Point mostra quando a placa nao tem produto cadastrado.
VALORES_PADRAO = (2.0, 5.0, 10.0, 20.0)
METODOS = {"credito", "debito", "pix"}


def so_digitos(valor: str | None) -> str:
    return re.sub(r"\D", "", valor or "")


def cnpj_valido(cnpj: str) -> bool:
    """Aceita CNPJ (14) ou CPF (11), so digitos, como o SDK espera."""
    return len(cnpj) in (11, 14) and len(set(cnpj)) > 1


# ---- Chave do terminal --------------------------------------------------

def autenticar_terminal(db: Session, chave: str | None) -> TerminalConfrapag:
    if not chave:
        raise HTTPException(status_code=401, detail="Terminal sem chave")
    terminal = (
        db.query(TerminalConfrapag)
        .filter(TerminalConfrapag.chave_hash == hash_chave(chave), TerminalConfrapag.ativo.is_(True))
        .first()
    )
    if not terminal:
        raise HTTPException(status_code=401, detail="Chave do terminal invalida")
    terminal.ultimo_uso_em = datetime.utcnow()
    db.commit()
    return terminal


def maquina_do_terminal(db: Session, terminal: TerminalConfrapag, machine_id: str) -> Maquina:
    """So maquinas do mesmo cliente e com banco confrapag_sdk. Qualquer outra
    responde 404, sem revelar que existe."""
    maquina = (
        db.query(Maquina)
        .filter(
            Maquina.id_hardware == machine_id,
            Maquina.cliente_id == terminal.cliente_id,
            Maquina.banco_pagamento == BANCO_CONFRAPAG,
        )
        .first()
    )
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina nao encontrada para este terminal")
    return maquina


def produtos_da_maquina(db: Session, maquina: Maquina) -> list[dict]:
    produtos = db.query(Produto).filter(Produto.maquina_id == maquina.id_hardware).order_by(Produto.valor).all()
    if produtos:
        return [{"id": p.id, "nome": p.nome, "valor": float(p.valor)} for p in produtos]
    return [{"id": None, "nome": "", "valor": v} for v in VALORES_PADRAO]


VALORES_MAXIMO = 12
VALOR_MAXIMO = 1000.0


def salvar_valores(db: Session, maquina: Maquina, valores: list[float]) -> list[dict]:
    """Compatibilidade: so valores, sem nome."""
    return salvar_produtos(db, maquina, [("", v) for v in valores])


def salvar_produtos(db: Session, maquina: Maquina, itens: list[tuple[str, float]]) -> list[dict]:
    """Produtos (nome + valor) cadastrados na propria maquininha (configuracoes com PIN).

    Viram os produtos da maquina no sistema: o painel mostra os mesmos produtos e
    o pagamento continua so aceitando esses valores."""
    vistos: dict[tuple[str, float], tuple[str, float]] = {}
    for nome, valor in itens:
        nome = (nome or "").strip()[:40]
        valor = round(float(valor), 2)
        vistos.setdefault((nome.lower(), valor), (nome, valor))
    limpos = sorted(vistos.values(), key=lambda item: (item[1], item[0]))
    if not limpos:
        raise HTTPException(status_code=422, detail="Informe pelo menos um valor")
    if len(limpos) > VALORES_MAXIMO:
        raise HTTPException(status_code=422, detail=f"No maximo {VALORES_MAXIMO} valores")
    if any(v <= 0 or v > VALOR_MAXIMO for _, v in limpos):
        raise HTTPException(status_code=422, detail=f"Cada valor precisa ser maior que zero e ate R$ {VALOR_MAXIMO:.0f}")

    db.query(Produto).filter(Produto.maquina_id == maquina.id_hardware).delete(synchronize_session=False)
    for nome, valor in limpos:
        nome = nome or f"R$ {valor:.2f}".replace(".", ",")
        db.add(Produto(nome=nome, valor=valor, maquina_id=maquina.id_hardware))
    db.commit()
    return produtos_da_maquina(db, maquina)


# ---- Pagamento ------------------------------------------------------------

def _validar_referencia(maquina: Maquina, referencia: str) -> None:
    # Formato exato: so prefixo deixaria a placa "X" aceitar a referencia da "X-2".
    match = re.fullmatch(rf"CP-{re.escape(maquina.id_hardware)}-(\d{{10,16}})", referencia)
    if not match:
        raise HTTPException(status_code=422, detail="Referencia de pagamento invalida para esta maquina")
    gerada_em = datetime.utcfromtimestamp(int(match.group(1)) / 1000)
    agora = datetime.utcnow()
    if agora - gerada_em > REFERENCIA_IDADE_MAXIMA or gerada_em - agora > REFERENCIA_FUTURO_TOLERANCIA:
        raise HTTPException(status_code=409, detail="Pagamento antigo demais para liberar credito")


def _validar_valor(db: Session, maquina: Maquina, valor: float) -> float:
    permitidos = [p["valor"] for p in produtos_da_maquina(db, maquina)]
    for permitido in permitidos:
        if abs(permitido - float(valor)) <= 0.001:
            return permitido
    raise HTTPException(status_code=422, detail="Valor nao cadastrado para esta maquina")


def registrar_pagamento_e_liberar(
    db: Session,
    terminal: TerminalConfrapag,
    maquina: Maquina,
    *,
    tid: str,
    nsu: str | None,
    referencia: str,
    valor: float,
    metodo: str | None,
    bandeira: str | None,
    autorizacao: str | None,
    sn_terminal: str | None,
    teste: bool = False,
) -> dict:
    tid = (tid or "").strip()
    if not tid:
        raise HTTPException(status_code=422, detail="Venda sem TID")
    if teste:
        # Pulso sem cartao: so com a chave marcada como teste no painel.
        if not terminal.modo_teste:
            raise HTTPException(status_code=403, detail="Esta chave nao esta liberada para pagamento de teste")
        if not tid.startswith("TESTE-"):
            tid = f"TESTE-{tid}"
    _validar_referencia(maquina, referencia)
    amount = _validar_valor(db, maquina, valor)
    metodo = (metodo or "").strip().lower()
    metodo = metodo if metodo in METODOS or (teste and metodo == "teste") else None
    machine_id = maquina.id_hardware

    _acquire_payment_lock(db, f"confrapag_tid_{tid}")
    duplicado = (
        db.query(VendaPagamento)
        .filter(VendaPagamento.provider == PROVIDER, VendaPagamento.provider_payment_id == tid)
        .first()
    )
    if duplicado:
        db.rollback()  # solta a trava
        if duplicado.maquina_id != machine_id:
            raise HTTPException(status_code=409, detail="Pagamento ja usado em outra maquina")
        return {
            "ok": True,
            "duplicado": True,
            "maquina_id": machine_id,
            "tid": tid,
            "valor": duplicado.valor_bruto,
            "pulsos": calcular_pulsos_por_valor(duplicado.valor_bruto),
            "command_id": duplicado.command_id,
            "command_status": (get_command_status(duplicado.command_id) if duplicado.command_id else None) or "pendente",
        }

    command_id = str(uuid4())
    agora = datetime.utcnow()
    tipo_pagamento = {"credito": "credit_card", "debito": "debit_card", "pix": "pix", "teste": "teste"}.get(metodo or "")
    transacao = Transacao(
        maquina_id=machine_id,
        tipo=EventoTipo.in_flux,
        metodo=MetodoPagamento.digital,
        valor=amount,
        data_hora=agora,
    )
    db.add(transacao)
    historico = HistoricoOperacao(
        maquina_id=machine_id,
        categoria="PAGAMENTO",
        descricao=(
            f"{'TESTE sem cartao ConfraPag' if teste else 'Pagamento ConfraPag SDK aprovado'} (tid={tid}, nsu={nsu or 'n/a'}, aut={autorizacao or 'n/a'}, "
            f"terminal_id={sn_terminal or 'n/a'}, terminal_app={terminal.nome}, metodo={metodo or 'n/a'}, "
            f"ref={referencia})"
        ),
        valor=amount,
        provider=PROVIDER,
        provider_payment_id=tid,
        payment_type=tipo_pagamento,
        card_brand=(bandeira or "").strip() or None,
        command_id=command_id,
        created_at=agora,
    )
    db.add(historico)
    db.flush()
    registrar_venda_pagamento(
        db,
        maquina_id=machine_id,
        valor=amount,
        origem=PROVIDER,
        transacao_id=transacao.id,
        historico_id=historico.id,
        provider=PROVIDER,
        provider_payment_id=tid,
        tipo_pagamento=tipo_pagamento,
        bandeira_cartao=historico.card_brand,
        banco="ConfraPag",
        status_pulso="pendente",
        command_id=command_id,
        conta_faturamento=not teste,
        conta_ticket_medio=not teste,
        is_teste=teste,
        created_at=agora,
    )
    db.commit()

    pulsos = calcular_pulsos_por_valor(amount)
    try:
        publish_machine_credit_pulses(machine_id, pulses=pulsos, action="paid", command_id=command_id, amount=amount)
    except Exception as exc:
        update_pulse_status(command_id, "falha_publicacao")
        raise HTTPException(status_code=502, detail="Falha ao enviar comando MQTT para a maquina") from exc

    command_status = get_command_status(command_id) or "pendente"
    print(
        f"[ConfraPag] pagamento processado tid={tid} terminal_app={terminal.id} "
        f"machine={machine_id} amount={amount} pulsos={pulsos} command_status={command_status}"
    )
    return {
        "ok": True,
        "duplicado": False,
        "maquina_id": machine_id,
        "tid": tid,
        "valor": amount,
        "pulsos": pulsos,
        "command_id": command_id,
        "command_status": command_status,
    }


# ---- Pix (API Confrapix) --------------------------------------------------
# O SDK da maquininha nao faz Pix: a cobranca e' criada aqui na Confrapix com o
# token do proprio cliente, a maquininha mostra o QR e o pulso so sai depois de
# a cobranca ser reconsultada como paga (o webhook deles nao tem assinatura).

PIX_VALIDADE = timedelta(minutes=5)
# Horario de Brasilia (sem horario de verao desde 2019); expiration_date vai nesse fuso.
FUSO_CONFRAPIX = timezone(timedelta(hours=-3))


def token_confrapix(db: Session, cliente_id: int) -> str | None:
    cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    token = (cliente.confrapix_token or "").strip() if cliente else ""
    return token or None


def pix_habilitado(db: Session, terminal: TerminalConfrapag) -> bool:
    return token_confrapix(db, terminal.cliente_id) is not None


def serializar_pix(cobranca: CobrancaPixConfrapag) -> dict:
    return {
        "id": cobranca.id,
        "status": cobranca.status,
        "valor": cobranca.valor,
        "codigo": cobranca.codigo,
        "expira_em": cobranca.expira_em.isoformat() if cobranca.expira_em else None,
        "pago": cobranca.status in confrapix.STATUS_PAGO,
        "command_id": cobranca.command_id,
        "command_status": (get_command_status(cobranca.command_id) or "pendente") if cobranca.command_id else None,
    }


def criar_pix(db: Session, terminal: TerminalConfrapag, maquina: Maquina, valor: float, referencia: str,
              callback_url: str | None) -> CobrancaPixConfrapag:
    _validar_referencia(maquina, referencia)
    amount = _validar_valor(db, maquina, valor)
    token = token_confrapix(db, terminal.cliente_id)
    if not token:
        raise HTTPException(status_code=422, detail="Pix nao configurado para este cliente (token Confrapix)")

    expira_local = datetime.now(FUSO_CONFRAPIX) + PIX_VALIDADE
    transacao = confrapix.criar_cobranca(
        token,
        amount,
        descricao=f"CompactPay {maquina.id_hardware} {referencia}"[:120],
        expiracao=expira_local.strftime("%Y-%m-%d %H:%M:%S"),
        callback_url=callback_url,
    )
    pix = transacao.get("pix") or {}
    cobranca = CobrancaPixConfrapag(
        terminal_id=terminal.id,
        cliente_id=terminal.cliente_id,
        maquina_id=maquina.id_hardware,
        referencia=referencia,
        valor=amount,
        confrapix_id=str(transacao["id"]),
        txid=pix.get("txid"),
        codigo=pix.get("code"),
        status=(transacao.get("status") or "processing").lower(),
        expira_em=datetime.utcnow() + PIX_VALIDADE,
    )
    db.add(cobranca)
    db.commit()
    db.refresh(cobranca)
    print(f"[ConfraPag Pix] cobranca criada id={cobranca.id} confrapix_id={cobranca.confrapix_id} machine={maquina.id_hardware} valor={amount}")
    return cobranca


def atualizar_pix(db: Session, cobranca: CobrancaPixConfrapag) -> CobrancaPixConfrapag:
    """Reconsulta na Confrapix e, se paga, libera o pulso (uma vez so: o TID e' PIX-<id>)."""
    if cobranca.command_id or cobranca.status in confrapix.STATUS_FINAIS_SEM_PAGAMENTO:
        return cobranca
    token = token_confrapix(db, cobranca.cliente_id)
    if not token:
        return cobranca
    transacao = confrapix.consultar(token, cobranca.confrapix_id)
    status = (transacao.get("status") or cobranca.status).lower()
    cobranca.status = status
    if status not in confrapix.STATUS_PAGO:
        db.commit()
        return cobranca

    valor_pago = float(transacao.get("amount") or 0)
    if abs(valor_pago - cobranca.valor) > 0.01:
        print(f"[ConfraPag Pix] valor divergente cobranca={cobranca.id} esperado={cobranca.valor} pago={valor_pago}")
        cobranca.status = "valor_divergente"
        db.commit()
        return cobranca

    terminal = db.query(TerminalConfrapag).filter(TerminalConfrapag.id == cobranca.terminal_id).first()
    maquina = db.query(Maquina).filter(Maquina.id_hardware == cobranca.maquina_id).first()
    resultado = registrar_pagamento_e_liberar(
        db,
        terminal,
        maquina,
        tid=f"PIX-{cobranca.confrapix_id}",
        nsu=cobranca.txid or (transacao.get("pix") or {}).get("endtoend_id"),
        referencia=cobranca.referencia,
        valor=cobranca.valor,
        metodo="pix",
        bandeira=None,
        autorizacao=None,
        sn_terminal=None,
    )
    db.refresh(cobranca)
    cobranca.status = status
    cobranca.command_id = resultado["command_id"]
    cobranca.pago_em = datetime.utcnow()
    db.commit()
    return cobranca


def cancelar_pix(db: Session, cobranca: CobrancaPixConfrapag) -> CobrancaPixConfrapag:
    """Cliente desistiu ou o tempo acabou. Se foi pago bem nessa hora, libera em vez de cancelar."""
    cobranca = atualizar_pix(db, cobranca)
    if cobranca.command_id or cobranca.status != "processing":
        return cobranca
    token = token_confrapix(db, cobranca.cliente_id)
    if token:
        try:
            confrapix.cancelar(token, cobranca.confrapix_id)
        except HTTPException:
            pass
    return atualizar_pix(db, cobranca)

"""Banco "mercado_pago_point": tela touch (ESP32) + Point em modo integrado (PDV).

Fluxo:
  tela -> POST /tela/cobrancas {produto_id}
       -> backend cria a order na Point (POST /v1/orders, type=point)
       -> cliente paga na Point
       -> webhook "order.processed" (mercado_pago_webhook.py, SEM alteracao)
          registra a venda e manda o pulso pela fila MQTT de sempre.

O valor sempre sai do produto cadastrado, nunca da tela. Enquanto espera, a tela
consulta GET /tela/cobrancas/{order_id}; se a order ja estiver paga e o webhook
ainda nao tiver chegado, a consulta chama o mesmo processamento do webhook (que
tem trava e checagem de duplicidade), entao o credito sai uma vez so.
"""
import hashlib
import secrets
import time
import urllib.parse
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import Cliente, EscutaTerminal, HistoricoOperacao, Maquina, Usuario, VendaPagamento
from app.models.produto import Produto
from app.models.tela_point import TelaPoint
from app.services.command_queue import get_command_status
from app.services.mercado_pago import create_pos_for_machine, mp_request
from app.services.pagamentos_helpers import parse_machine_id_from_external_reference

BANCO_TELA_POINT = "mercado_pago_point"
MP_API = "https://api.mercadopago.com"

# Status da order (API de Orders) -> o que a tela precisa saber.
STATUS_AGUARDANDO = {"created", "at_terminal", "action_required"}
STATUS_PAGO = {"processed"}
STATUS_ENCERRADO = {"canceled", "cancelled", "expired", "failed", "refunded"}


# ---- Chave da tela ---------------------------------------------------------

def gerar_chave() -> str:
    return "cptela_" + secrets.token_urlsafe(32)


def hash_chave(chave: str) -> str:
    return hashlib.sha256(chave.encode("utf-8")).hexdigest()


def autenticar_tela(db: Session, chave: str | None) -> TelaPoint:
    if not chave:
        raise HTTPException(status_code=401, detail="Tela sem chave")
    tela = (
        db.query(TelaPoint)
        .filter(TelaPoint.chave_hash == hash_chave(chave), TelaPoint.ativo.is_(True))
        .first()
    )
    if not tela:
        raise HTTPException(status_code=401, detail="Chave da tela invalida")
    tela.ultimo_uso_em = datetime.utcnow()
    db.commit()
    return tela


def maquina_da_tela(db: Session, tela: TelaPoint) -> Maquina:
    """A maquina precisa continuar do mesmo cliente e com o banco desta integracao;
    se alguem trocar o banco no painel, a tela para de cobrar."""
    maquina = (
        db.query(Maquina)
        .filter(
            Maquina.id_hardware == tela.maquina_id,
            Maquina.cliente_id == tela.cliente_id,
            Maquina.banco_pagamento == BANCO_TELA_POINT,
        )
        .first()
    )
    if not maquina:
        raise HTTPException(status_code=404, detail="Maquina desta tela nao esta disponivel")
    return maquina


def token_do_cliente(db: Session, cliente_id: int) -> str:
    cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    token = (cliente.mp_access_token or "").strip() if cliente else ""
    if not token:
        raise HTTPException(status_code=422, detail="Cliente sem credencial Mercado Pago")
    return token


# ---- Point (modo integrado) -----------------------------------------------

MSG_SEM_PERMISSAO_POINT = (
    "O Mercado Pago recusou o acesso as maquininhas Point desta conta (403). "
    "Confira se a credencial do cliente e' a de producao da propria conta dona da Point "
    "e se a aplicacao do Mercado Pago tem o produto Point / pagamentos presenciais habilitado."
)


def _recusado_pelo_mp(exc: HTTPException) -> bool:
    detalhe = str(exc.detail or "")
    return "erro 403" in detalhe or "erro 401" in detalhe or "UNAUTHORIZED" in detalhe


# ---- Tokens da conta Mercado Pago do cliente -------------------------------------
# O token salvo do cliente (OAuth do "Conectar Mercado Pago") as vezes le a Point
# mas e' recusado ao operar a maquininha (403 PA_UNAUTHORIZED_RESULT_FROM_POLICIES).
# Nesse caso tenta os outros tokens que o sistema ja guarda (o principal do
# backend e os dos usuarios do cliente), MAS so os que forem da MESMA conta MP
# do cliente (conferido em /users/me) - nunca mexe na Point com a conta de outro.

_conta_por_token: dict[str, str] = {}


def _conta_do_token(token: str) -> str:
    chave = hash_chave(token)
    if chave not in _conta_por_token:
        try:
            _conta_por_token[chave] = str(mp_request("GET", f"{MP_API}/users/me", token).get("id") or "")
        except HTTPException:
            return ""  # nao guarda falha: pode ter sido instabilidade
    return _conta_por_token[chave]


def _tokens_extras(db: Session, cliente: Cliente) -> list[str]:
    candidatos = [settings.MP_ACCESS_TOKEN]
    candidatos += [
        u[0] for u in db.query(Usuario.mp_access_token)
        .filter(Usuario.cliente_id == cliente.id, Usuario.mp_access_token.isnot(None))
        .all()
    ]
    vistos = {(cliente.mp_access_token or "").strip()}
    extras = []
    for token in candidatos:
        token = (token or "").strip()
        if token and token not in vistos:
            vistos.add(token)
            extras.append(token)
    return extras


def chamar_mp(db: Session, cliente_id: int, method: str, url: str, body: dict | None = None,
              headers: dict | None = None) -> dict:
    """mp_request com o token do cliente; se o MP recusar por permissao, repete
    com outro token salvo da mesma conta MP. Outros erros sobem na hora."""
    principal = token_do_cliente(db, cliente_id)
    try:
        return mp_request(method, url, principal, body=body, headers=headers)
    except HTTPException as exc:
        if not _recusado_pelo_mp(exc):
            raise
        ultimo = exc
    cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    conta = (cliente.mp_user_id or "").strip() or _conta_do_token(principal)
    if not conta:
        raise ultimo
    for token in _tokens_extras(db, cliente):
        if _conta_do_token(token) != conta:
            continue
        try:
            resposta = mp_request(method, url, token, body=body, headers=headers)
            caminho = url.split(".com", 1)[-1]
            print(f"[Tela Point] {method} {caminho} recusado com o token do cliente {cliente_id}; "
                  f"feito com outro token da mesma conta MP {conta}")
            return resposta
        except HTTPException as exc:
            if not _recusado_pelo_mp(exc):
                raise
            ultimo = exc
    raise ultimo


def _normalizar_terminais(lista) -> list[dict]:
    return [
        {
            "id": t.get("id"),
            "operating_mode": t.get("operating_mode"),
            "pos_id": t.get("pos_id"),
            "store_id": t.get("store_id"),
            "external_pos_id": t.get("external_pos_id"),
        }
        for t in (lista or [])
        if isinstance(t, dict) and t.get("id")
    ]


def listar_terminais_point(db: Session, cliente_id: int) -> list[dict]:
    """Points da conta do cliente, com o modo de operacao (PDV = integrado).

    Tenta a API nova (/terminals/v1) e, se o MP recusar por permissao, a antiga
    (/point/integration-api/devices), que algumas contas/aplicacoes ainda usam."""
    try:
        data = chamar_mp(db, cliente_id, "GET", f"{MP_API}/terminals/v1/list?limit=50")
        return _normalizar_terminais(((data.get("data") or {}).get("terminals")) if isinstance(data, dict) else None)
    except HTTPException as exc:
        if not _recusado_pelo_mp(exc):
            raise
        print(f"[Tela Point] /terminals/v1/list recusado, tentando API antiga: {exc.detail}")
    try:
        data = chamar_mp(db, cliente_id, "GET", f"{MP_API}/point/integration-api/devices?limit=50")
    except HTTPException as exc:
        if _recusado_pelo_mp(exc):
            raise HTTPException(status_code=403, detail=MSG_SEM_PERMISSAO_POINT) from exc
        raise
    return _normalizar_terminais(data.get("devices") if isinstance(data, dict) else None)


def ativar_modo_pdv(db: Session, cliente_id: int, terminal_id: str) -> dict:
    """Coloca a Point em modo integrado. Depois disso ela so cobra o que vier da
    API (precisa reiniciar a Point para aplicar). Mesma ordem: API nova, depois a antiga."""
    try:
        return chamar_mp(
            db, cliente_id,
            "PATCH",
            f"{MP_API}/terminals/v1/setup",
            body={"terminals": [{"id": terminal_id, "operating_mode": "PDV"}]},
        )
    except HTTPException as exc:
        if not _recusado_pelo_mp(exc):
            raise
    try:
        return chamar_mp(
            db, cliente_id,
            "PATCH",
            f"{MP_API}/point/integration-api/devices/{urllib.parse.quote(terminal_id)}",
            body={"operating_mode": "PDV"},
        )
    except HTTPException as exc:
        if _recusado_pelo_mp(exc):
            raise HTTPException(status_code=403, detail=MSG_SEM_PERMISSAO_POINT) from exc
        raise


def caixa_da_maquina(maquina: Maquina) -> dict | None:
    if not (maquina.mp_pos_id or "").strip():
        return None
    return {
        "pos_id": maquina.mp_pos_id,
        "pos_external_id": maquina.mp_pos_external_id,
        "store_id": maquina.mp_store_id,
        "store_external_id": maquina.mp_store_external_id,
    }


def criar_caixa(db: Session, maquina: Maquina) -> dict:
    """Mesmo caixa (loja + POS) que o banco "mercado_pago" cria ao cadastrar a
    maquina. Serve para maquinas com banco de tela que nasceram sem caixa.
    create_pos_for_machine reaproveita o caixa se ele ja existir no MP."""
    existente = caixa_da_maquina(maquina)
    if existente:
        return existente
    cliente = db.query(Cliente).filter(Cliente.id == maquina.cliente_id).first()
    if not cliente or not (cliente.mp_access_token or "").strip():
        raise HTTPException(status_code=422, detail="Cliente sem credencial Mercado Pago")
    pos = create_pos_for_machine(cliente, maquina)
    maquina.mp_store_id = pos["mp_store_id"]
    maquina.mp_store_external_id = pos["mp_store_external_id"]
    maquina.mp_pos_id = pos["mp_pos_id"]
    maquina.mp_pos_external_id = pos["mp_pos_external_id"]
    maquina.mp_qr_image = pos["mp_qr_image"]
    db.commit()
    return caixa_da_maquina(maquina)


def point_no_caixa(point: dict, maquina: Maquina) -> bool:
    """A Point esta vinculada ao caixa desta maquina?"""
    pos_id = str(point.get("pos_id") or "").strip()
    external_pos_id = str(point.get("external_pos_id") or "").strip()
    return bool(
        (pos_id and pos_id == str(maquina.mp_pos_id or "").strip())
        or (external_pos_id and external_pos_id == str(maquina.mp_pos_external_id or "").strip())
    )


def _variantes_terminal(terminal_id: str) -> set[str]:
    """O id da Point na API de Orders e' "MODELO__SERIAL"; no pagamento o MP as
    vezes manda so o serial. Compara pelas duas formas."""
    terminal_id = (terminal_id or "").strip()
    variantes = {terminal_id}
    if "__" in terminal_id:
        variantes.add(terminal_id.split("__", 1)[1])
    return {v for v in variantes if v}


def validar_point_livre(db: Session, maquina: Maquina, terminal_id: str) -> None:
    """Barra a Point que tambem dispararia o aviso de "payment" do webhook antigo
    para uma maquina - senao o mesmo pagamento liberaria credito duas vezes
    (uma pela order, outra pelo payment). O webhook nao foi alterado; a
    protecao fica aqui, no cadastro da tela."""
    variantes = _variantes_terminal(terminal_id)
    escuta = (
        db.query(EscutaTerminal)
        .filter(EscutaTerminal.terminal_id.in_(variantes), EscutaTerminal.ativo.is_(True))
        .first()
    )
    if escuta:
        raise HTTPException(
            status_code=409,
            detail=f"Esta Point esta com escuta ativa na maquina {escuta.maquina_id}. Desative a escuta antes de usar com a tela.",
        )

    point = next((t for t in listar_terminais_point(db, maquina.cliente_id) if t.get("id") == terminal_id), None)
    if not point:
        raise HTTPException(status_code=404, detail="Point nao encontrada na conta Mercado Pago deste cliente")
    pos_id = str(point.get("pos_id") or "").strip()
    external_pos_id = str(point.get("external_pos_id") or "").strip()
    if pos_id or external_pos_id:
        filtros = []
        if pos_id:
            filtros.append(Maquina.mp_pos_id == pos_id)
        if external_pos_id:
            filtros.append(Maquina.mp_pos_external_id == external_pos_id)
        outra = (
            db.query(Maquina)
            .filter(or_(*filtros), Maquina.id_hardware != maquina.id_hardware)
            .first()
        )
        if outra:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Esta Point esta no mesmo caixa Mercado Pago da maquina {outra.id_hardware}. "
                    "Vincule a Point a um caixa proprio antes de usar com a tela."
                ),
            )


# ---- Cobranca --------------------------------------------------------------

def produto_da_maquina(db: Session, maquina: Maquina, produto_id: int) -> Produto:
    produto = (
        db.query(Produto)
        .filter(Produto.id == produto_id, Produto.maquina_id == maquina.id_hardware)
        .first()
    )
    if not produto or not produto.valor or float(produto.valor) <= 0:
        raise HTTPException(status_code=404, detail="Valor nao encontrado para esta maquina")
    return produto


def criar_cobranca(db: Session, tela: TelaPoint, maquina: Maquina, produto: Produto) -> dict:
    valor = round(float(produto.valor), 2)
    # Mesmo formato que o webhook ja sabe ler: MACHINE_ID:timestamp.
    external_reference = f"{maquina.id_hardware}:{int(time.time() * 1000)}"
    body = {
        "type": "point",
        "external_reference": external_reference,
        "description": f"{produto.nome or 'Credito'} - {maquina.nome_local or maquina.id_hardware}"[:150],
        "transactions": {"payments": [{"amount": f"{valor:.2f}"}]},
        "config": {
            "point": {
                "terminal_id": tela.point_terminal_id,
                "print_on_terminal": "no_ticket",
            }
        },
    }
    order = chamar_mp(
        db, tela.cliente_id,
        "POST",
        f"{MP_API}/v1/orders",
        body=body,
        headers={"X-Idempotency-Key": str(uuid4())},
    )
    order_id = order.get("id")
    if not order_id:
        raise HTTPException(status_code=502, detail="Mercado Pago nao retornou o id da cobranca")

    db.add(
        HistoricoOperacao(
            maquina_id=maquina.id_hardware,
            # Categoria propria: nao entra em relatorio de faturamento (so a venda paga entra).
            categoria="COBRANCA_TELA",
            descricao=(
                f"Cobranca enviada pela tela para a Point (mp_order_id={order_id}, "
                f"terminal_id={tela.point_terminal_id}, tela={tela.nome}, produto_id={produto.id})"
            ),
            valor=valor,
            created_at=datetime.utcnow(),
        )
    )
    db.commit()
    print(f"[Tela Point] order criada {order_id} machine={maquina.id_hardware} valor={valor} terminal={tela.point_terminal_id}")
    return {"order_id": order_id, "status": order.get("status") or "created", "valor": valor}


def _order_da_maquina(db: Session, cliente_id: int, maquina: Maquina, order_id: str) -> dict:
    order = chamar_mp(db, cliente_id, "GET", f"{MP_API}/v1/orders/{urllib.parse.quote(order_id)}")
    if parse_machine_id_from_external_reference(order.get("external_reference")) != maquina.id_hardware:
        # Nao revela que a order existe se ela for de outra maquina.
        raise HTTPException(status_code=404, detail="Cobranca nao encontrada")
    return order


def _venda_da_order(db: Session, order_id: str) -> VendaPagamento | None:
    return (
        db.query(VendaPagamento)
        .filter(VendaPagamento.provider == "mercado_pago", VendaPagamento.provider_payment_id == str(order_id))
        .first()
    )


def consultar_cobranca(db: Session, tela: TelaPoint, maquina: Maquina, order_id: str) -> dict:
    order = _order_da_maquina(db, tela.cliente_id, maquina, order_id)
    status_mp = (order.get("status") or "").lower()

    if status_mp in STATUS_PAGO:
        venda = _venda_da_order(db, order_id)
        if not venda:
            # Webhook ainda nao chegou (ou nao esta configurado): processa por aqui.
            # Import local: o webhook importa muita coisa e so e' preciso neste caso.
            from app.services.mercado_pago_webhook import processar_callback_mercado_pago

            processar_callback_mercado_pago({"type": "order", "action": "order.processed", "data": {"id": order_id}})
            db.expire_all()
            venda = _venda_da_order(db, order_id)
        command_id = venda.command_id if venda else None
        return {
            "order_id": order_id,
            "situacao": "pago",
            "status_mp": status_mp,
            "command_id": command_id,
            "command_status": (get_command_status(command_id) if command_id else None) or "pendente",
        }

    if status_mp in STATUS_ENCERRADO:
        situacao = "cancelado"
    else:
        situacao = "aguardando"
    return {"order_id": order_id, "situacao": situacao, "status_mp": status_mp}


def cancelar_cobranca(db: Session, tela: TelaPoint, maquina: Maquina, order_id: str) -> dict:
    """Cliente desistiu ou a tela deu timeout. O MP so cancela pela API antes de a
    Point mostrar a cobranca; depois disso ela tem que ser cancelada na propria Point."""
    _order_da_maquina(db, tela.cliente_id, maquina, order_id)
    order = chamar_mp(
        db, tela.cliente_id,
        "POST",
        f"{MP_API}/v1/orders/{urllib.parse.quote(order_id)}/cancel",
        headers={"X-Idempotency-Key": str(uuid4())},
    )
    return {"order_id": order_id, "status_mp": (order.get("status") or "").lower()}

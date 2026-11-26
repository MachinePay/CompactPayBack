"""Logica do totem vending (ver app/models/totem.py).

Contrato com o agente da maquina (projeto VendingMachine, backend/src/sync.js):
  POST /totem/vincular              {code, agentVersion, boardVersion} -> {machineId, token, name}
  POST /totem/{id}/eventos          {events:[{id,type,payload,createdAt}]} -> {accepted:[ids]}
  GET  /totem/{id}/config?version=  -> 204 | {version, slots, payment}
  GET  /totem/midias/{hash}.webp    foto do produto

As cobrancas de cartao do totem saem do proprio agente com referencia
"kiosk-<id do pedido>". O webhook do Mercado Pago e o poller da SumUp ignoram
essas referencias (e as maquininhas cadastradas em totens), entao nada disso
libera pulso em outra maquina.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.models import EventoTipo, Maquina, MetodoPagamento, Transacao, VendaPagamento
from app.models.totem import TotemEvento, TotemMidia, TotemProduto, TotemVending, TotemVideo
from app.services.vendas import registrar_venda_pagamento

PREFIXO_REFERENCIA = "kiosk-"
SLOTS = 9
CODIGO_VALIDADE = timedelta(hours=24)
CODIGO_ALFABETO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MODOS_CARTAO = {"off", "mercadopago", "sumup"}
MIDIA_MAX = 2 * 1024 * 1024
STATUS_PULSO_TOTEM = "totem_entregue"  # fora de PAYMENT_PENDING_PULSE_STATUSES: nao gera alerta


# Bancos da maquina que sao totem (cadastro de maquina, permissao "Vending machine" no cliente).
BANCOS_TOTEM = {"mercado_pago_totem": "mercadopago", "sumup_totem": "sumup"}


def modo_cartao_efetivo(totem: TotemVending, maquina: Maquina | None) -> str:
    """O banco da maquina manda: Mercado Pago/SumUp (vending machine) fixa a maquininha."""
    if maquina is not None and maquina.banco_pagamento in BANCOS_TOTEM:
        return BANCOS_TOTEM[maquina.banco_pagamento]
    return totem.modo_cartao or "off"


def _sha256(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def eh_referencia_totem(referencia) -> bool:
    return isinstance(referencia, str) and referencia.startswith(PREFIXO_REFERENCIA)


def terminais_mp_de_totem(db: Session) -> set[str]:
    rows = db.query(TotemVending.mp_terminal_id).filter(TotemVending.mp_terminal_id.isnot(None)).all()
    return {r[0] for r in rows if r[0]}


def readers_sumup_de_totem(db: Session) -> set[str]:
    rows = db.query(TotemVending.sumup_reader_id).filter(TotemVending.sumup_reader_id.isnot(None)).all()
    return {r[0] for r in rows if r[0]}


# ---------------------------------------------------------------- cadastro (painel)
def obter_totem(db: Session, maquina_id: str) -> TotemVending | None:
    return db.query(TotemVending).filter(TotemVending.maquina_id == maquina_id).first()


def ativar(db: Session, maquina: Maquina) -> TotemVending:
    if not maquina.cliente_id:
        raise HTTPException(status_code=422, detail="Vincule a maquina a um cliente antes de ativar o totem")
    totem = obter_totem(db, maquina.id_hardware)
    if totem is None:
        totem = TotemVending(cliente_id=maquina.cliente_id, maquina_id=maquina.id_hardware, modo_cartao="off")
        db.add(totem)
        db.flush()
        for slot in range(SLOTS):
            db.add(TotemProduto(maquina_id=maquina.id_hardware, slot=slot, nome=f"Produto {slot + 1}",
                                preco_centavos=500, ordem=slot, atualizado_por="painel"))
    totem.ativo = True
    totem.cliente_id = maquina.cliente_id
    db.flush()
    return totem


def gerar_codigo(db: Session, totem: TotemVending) -> dict:
    codigo = "".join(secrets.choice(CODIGO_ALFABETO) for _ in range(8))
    totem.codigo_hash = _sha256(codigo)
    totem.codigo_expira_em = datetime.utcnow() + CODIGO_VALIDADE
    db.flush()
    return {"codigo": codigo, "expira_em": totem.codigo_expira_em.isoformat() + "Z"}


def desativar(db: Session, totem: TotemVending) -> None:
    totem.ativo = False
    totem.token_hash = None
    totem.token_prefixo = None
    totem.codigo_hash = None
    totem.codigo_expira_em = None
    db.flush()


def resumo(db: Session, totem: TotemVending | None, maquina: Maquina) -> dict:
    dono = maquina.dono
    if totem is None:
        return {"ativo": False, "maquina_id": maquina.id_hardware}
    return {
        "ativo": bool(totem.ativo),
        "maquina_id": totem.maquina_id,
        "vinculado": bool(totem.token_hash),
        "vinculado_em": totem.vinculado_em.isoformat() + "Z" if totem.vinculado_em else None,
        "ultimo_contato": totem.ultimo_contato.isoformat() + "Z" if totem.ultimo_contato else None,
        "versao_agente": totem.versao_agente,
        "versao_placa": totem.versao_placa,
        "codigo_pendente": bool(totem.codigo_hash and totem.codigo_expira_em and totem.codigo_expira_em > datetime.utcnow()),
        "modo_cartao": modo_cartao_efetivo(totem, maquina),
        "modo_pelo_banco": maquina.banco_pagamento in BANCOS_TOTEM,
        "mp_terminal_id": totem.mp_terminal_id,
        "sumup_reader_id": totem.sumup_reader_id,
        "caixa": _caixa(maquina),
        "video": video_info(db, maquina.id_hardware),
        "credenciais_cliente": {
            "mercadopago": bool(dono and (dono.mp_access_token or "").strip()),
            "sumup": bool(dono and (dono.sumup_api_key or "").strip()),
        },
    }


def _caixa(maquina: Maquina) -> dict | None:
    if not (maquina.mp_pos_id or "").strip():
        return None
    return {"pos_id": maquina.mp_pos_id, "pos_external_id": maquina.mp_pos_external_id,
            "store_id": maquina.mp_store_id, "store_external_id": maquina.mp_store_external_id}


def salvar_pagamento(db: Session, totem: TotemVending, modo: str, mp_terminal_id: str | None, sumup_reader_id: str | None) -> None:
    if modo not in MODOS_CARTAO:
        raise HTTPException(status_code=422, detail="Modo de cartao invalido")
    if mp_terminal_id and db.query(TotemVending.id).filter(
        TotemVending.mp_terminal_id == mp_terminal_id, TotemVending.id != totem.id
    ).first():
        raise HTTPException(status_code=409, detail="Essa Point ja esta em outro totem")
    totem.modo_cartao = modo
    totem.mp_terminal_id = (mp_terminal_id or "").strip() or None
    totem.sumup_reader_id = (sumup_reader_id or "").strip() or None
    totem.config_versao = (totem.config_versao or 1) + 1
    db.flush()


def listar_produtos(db: Session, maquina_id: str) -> list[TotemProduto]:
    return db.query(TotemProduto).filter(TotemProduto.maquina_id == maquina_id).order_by(TotemProduto.slot).all()


def produto_dict(p: TotemProduto) -> dict:
    return {
        "slot": p.slot, "nome": p.nome, "preco_centavos": p.preco_centavos, "imagem_hash": p.imagem_hash,
        "ativo": bool(p.ativo), "estoque": p.estoque, "estoque_minimo": p.estoque_minimo, "ordem": p.ordem,
        "atualizado_em": p.atualizado_em.isoformat() + "Z" if p.atualizado_em else None, "atualizado_por": p.atualizado_por,
    }


def salvar_produto(db: Session, totem: TotemVending, slot: int, dados: dict, por: str) -> TotemProduto:
    if not 0 <= slot < SLOTS:
        raise HTTPException(status_code=422, detail="Baia invalida")
    p = db.query(TotemProduto).filter(TotemProduto.maquina_id == totem.maquina_id, TotemProduto.slot == slot).first()
    if p is None:
        p = TotemProduto(maquina_id=totem.maquina_id, slot=slot, nome=f"Produto {slot + 1}", ordem=slot)
        db.add(p)
    imagem = dados.get("imagem_hash", p.imagem_hash)
    if imagem and not db.query(TotemMidia.hash).filter(TotemMidia.hash == imagem).first():
        raise HTTPException(status_code=422, detail="Imagem nao encontrada")
    for campo in ("nome", "preco_centavos", "ativo", "estoque", "estoque_minimo", "ordem"):
        if campo in dados:
            setattr(p, campo, dados[campo])
    p.imagem_hash = imagem
    p.atualizado_em = datetime.utcnow()
    p.atualizado_por = por
    totem.config_versao = (totem.config_versao or 1) + 1
    db.flush()
    return p


def salvar_midia(db: Session, cliente_id: int | None, conteudo: bytes) -> str:
    if not conteudo or len(conteudo) > MIDIA_MAX:
        raise HTTPException(status_code=413, detail="Imagem vazia ou maior que 2 MB")
    if conteudo[:4] != b"RIFF" or conteudo[8:12] != b"WEBP":
        raise HTTPException(status_code=422, detail="Envie a imagem em WebP")
    h = hashlib.sha256(conteudo).hexdigest()
    if not db.query(TotemMidia.hash).filter(TotemMidia.hash == h).first():
        db.add(TotemMidia(hash=h, cliente_id=cliente_id, tamanho=len(conteudo), conteudo=conteudo))
        db.flush()
    return h


# ---------------------------------------------------------------- agente da maquina
def vincular(db: Session, codigo: str, versao_agente: str | None, versao_placa: str | None) -> dict:
    codigo = (codigo or "").strip().upper()
    agora = datetime.utcnow()
    totem = (
        db.query(TotemVending)
        .filter(TotemVending.codigo_hash == _sha256(codigo), TotemVending.ativo.is_(True))
        .first()
    )
    if totem is None or not totem.codigo_expira_em or totem.codigo_expira_em < agora:
        raise HTTPException(status_code=404, detail="Codigo de vinculo invalido ou expirado")
    token = "cptotem_" + secrets.token_urlsafe(32)
    totem.token_hash = _sha256(token)
    totem.token_prefixo = token[:14]
    totem.codigo_hash = None
    totem.codigo_expira_em = None
    totem.vinculado_em = agora
    totem.ultimo_contato = agora
    totem.versao_agente = (versao_agente or "")[:40] or None
    totem.versao_placa = (versao_placa or "")[:40] or None
    maquina = db.query(Maquina).filter(Maquina.id_hardware == totem.maquina_id).first()
    db.flush()
    return {"machineId": totem.maquina_id, "token": token, "name": (maquina.nome_local if maquina else None) or totem.maquina_id}


def autenticar(db: Session, maquina_id: str, authorization: str | None) -> TotemVending:
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Token do totem ausente")
    totem = (
        db.query(TotemVending)
        .filter(TotemVending.token_hash == _sha256(token), TotemVending.ativo.is_(True))
        .first()
    )
    if totem is None or not hmac.compare_digest(totem.maquina_id, maquina_id):
        raise HTTPException(status_code=401, detail="Token do totem invalido")
    return totem


def registrar_contato(db: Session, totem: TotemVending) -> None:
    agora = datetime.utcnow()
    totem.ultimo_contato = agora
    maquina = db.query(Maquina).filter(Maquina.id_hardware == totem.maquina_id).first()
    if maquina is not None:
        maquina.ultimo_sinal = agora  # totem aparece online na Saude das maquinas
    db.flush()


# ---------------------------------------------------------------- video da tela inicial
VIDEO_MAX = 80 * 1024 * 1024
VIDEO_TIPOS = {"video/mp4": "mp4", "video/webm": "webm"}


def tipo_do_video(cabecalho: bytes) -> str | None:
    """MP4 tem "ftyp" no byte 4; WebM comeca com a assinatura EBML."""
    if cabecalho[4:8] == b"ftyp":
        return "video/mp4"
    if cabecalho[:4] == bytes([0x1A, 0x45, 0xDF, 0xA3]):
        return "video/webm"
    return None


def video_da_maquina(db: Session, maquina_id: str) -> TotemVideo | None:
    return db.query(TotemVideo).filter(TotemVideo.maquina_id == maquina_id).first()


def video_info(db: Session, maquina_id: str) -> dict | None:
    v = video_da_maquina(db, maquina_id)
    if v is None:
        return None
    return {"hash": v.hash, "mime": v.mime, "tamanho": v.tamanho, "nome": v.nome_original,
            "enviado_em": v.created_at.isoformat() + "Z" if v.created_at else None}


def _video_config(db: Session, maquina_id: str) -> dict | None:
    v = video_da_maquina(db, maquina_id)
    return {"hash": v.hash, "ext": VIDEO_TIPOS.get(v.mime, "mp4")} if v else None


def salvar_video(db: Session, totem: TotemVending, conteudo: bytes, nome: str | None) -> TotemVideo:
    if not conteudo:
        raise HTTPException(status_code=422, detail="Arquivo de video vazio")
    mime = tipo_do_video(conteudo[:16])
    if mime is None:
        raise HTTPException(status_code=422, detail="Envie o video em MP4 (ou WebM)")
    db.query(TotemVideo).filter(TotemVideo.maquina_id == totem.maquina_id).delete()
    v = TotemVideo(maquina_id=totem.maquina_id, hash=hashlib.sha256(conteudo).hexdigest(), mime=mime,
                   tamanho=len(conteudo), nome_original=(nome or "")[:120] or None, conteudo=conteudo)
    db.add(v)
    totem.config_versao = (totem.config_versao or 1) + 1
    db.flush()
    return v


def montar_config(db: Session, totem: TotemVending) -> dict:
    produtos = listar_produtos(db, totem.maquina_id)
    dono = db.query(Maquina).filter(Maquina.id_hardware == totem.maquina_id).first()
    cliente = dono.dono if dono else None
    modo = modo_cartao_efetivo(totem, dono)
    payment = {"mode": modo}
    if modo == "mercadopago":
        payment["mercadopago"] = {"accessToken": (cliente.mp_access_token or "").strip() if cliente else "",
                                  "terminalId": totem.mp_terminal_id or ""}
    elif modo == "sumup":
        payment["sumup"] = {"apiKey": (cliente.sumup_api_key or "").strip() if cliente else "",
                            "merchantCode": (cliente.sumup_merchant_code or "").strip() if cliente else "",
                            "readerId": totem.sumup_reader_id or ""}
    corpo = {
        "slots": [
            {"slot": p.slot, "name": p.nome, "priceCents": int(p.preco_centavos), "imageHash": p.imagem_hash,
             "enabled": bool(p.ativo), "stock": p.estoque, "lowStock": p.estoque_minimo, "sortOrder": p.ordem}
            for p in produtos
        ],
        "payment": payment,
        "attractVideo": _video_config(db, totem.maquina_id),
    }
    # Versao = impressao digital do conteudo: muda tambem quando o token do
    # cliente e' renovado (OAuth do Mercado Pago), sem precisar de contador.
    corpo["version"] = hashlib.sha256(json.dumps(corpo, sort_keys=True).encode()).hexdigest()[:16]
    return corpo


def _data_evento(evento: dict) -> datetime:
    bruto = evento.get("createdAt") or (evento.get("payload") or {}).get("at")
    try:
        return datetime.fromisoformat(str(bruto).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return datetime.utcnow()


def processar_eventos(db: Session, totem: TotemVending, eventos: list[dict]) -> list[str]:
    """Grava os eventos (idempotente pelo id) e lanca no financeiro da maquina:
    nota/moeda -> entrada FISICO; cartao aprovado e entregue -> entrada DIGITAL;
    premio entregue -> saida (OUT). Pedido no cartao sem entrega nao conta."""
    aceitos: list[str] = []
    for ev in eventos[:500]:
        ev_id = str(ev.get("id") or "")[:64]
        tipo = str(ev.get("type") or "")[:40]
        payload = ev.get("payload") or {}
        if not ev_id or not tipo or not isinstance(payload, dict):
            continue
        if db.get(TotemEvento, ev_id) is not None:
            aceitos.append(ev_id)
            continue
        quando = _data_evento(ev)
        db.add(TotemEvento(id=ev_id, maquina_id=totem.maquina_id, tipo=tipo,
                           payload=json.dumps(payload, default=str)[:20000], criado_em=quando))
        _lancar(db, totem, tipo, payload, quando)
        db.flush()
        aceitos.append(ev_id)
    return aceitos


def _lancar(db: Session, totem: TotemVending, tipo: str, p: dict, quando: datetime) -> None:
    maquina_id = totem.maquina_id
    if tipo == "credit" and p.get("source") in {"note", "card"}:
        valor = int(p.get("amount") or 0) / 100
        if valor <= 0:
            return
        fisico = p.get("source") == "note"
        t = Transacao(maquina_id=maquina_id, tipo=EventoTipo.in_flux,
                      metodo=MetodoPagamento.fisico if fisico else MetodoPagamento.digital, valor=valor, data_hora=quando)
        db.add(t)
        db.flush()
        registrar_venda_pagamento(
            db, maquina_id=maquina_id, valor=valor, transacao_id=t.id, created_at=quando,
            origem="fisico" if fisico else "totem", provider="fisico" if fisico else "cartao_pulso",
            tipo_pagamento="nota" if fisico else "cartao", status_pulso="fisico" if fisico else STATUS_PULSO_TOTEM,
        )
    elif tipo == "order" and p.get("method") == "card" and p.get("status") == "done":
        ref = PREFIXO_REFERENCIA + str(p.get("id") or "")
        if db.query(VendaPagamento.id).filter(VendaPagamento.provider_payment_id == ref).first():
            return
        valor = int(p.get("priceCents") or 0) / 100
        if valor <= 0:
            return
        t = Transacao(maquina_id=maquina_id, tipo=EventoTipo.in_flux, metodo=MetodoPagamento.digital, valor=valor, data_hora=quando)
        db.add(t)
        db.flush()
        modo = modo_cartao_efetivo(totem, db.query(Maquina).filter(Maquina.id_hardware == maquina_id).first())
        provider = {"mercadopago": "mercado_pago", "sumup": "sumup"}.get(modo, modo)
        registrar_venda_pagamento(
            db, maquina_id=maquina_id, valor=valor, transacao_id=t.id, created_at=quando, origem="totem",
            provider=provider, provider_payment_id=ref, tipo_pagamento="cartao", status_pulso=STATUS_PULSO_TOTEM,
        )
    elif tipo == "dispense" and p.get("kind") == "sale":
        entregues = max(0, min(int(p.get("delivered") or 0), 50))
        # O totem baixa o estoque local na hora; aqui o painel acompanha, senao a
        # proxima config mandaria o estoque antigo de volta para a maquina.
        produto = db.query(TotemProduto).filter(TotemProduto.maquina_id == maquina_id, TotemProduto.slot == p.get("slot")).first()
        if produto is not None and produto.estoque is not None and entregues:
            produto.estoque = max(0, produto.estoque - entregues)
        for _ in range(entregues):
            # Mesmo formato da saida pelo sensor (mqtt_worker): uma linha OUT por premio, valor 0.
            db.add(Transacao(maquina_id=maquina_id, tipo=EventoTipo.out_flux, metodo=MetodoPagamento.fisico, valor=0.0, data_hora=quando))

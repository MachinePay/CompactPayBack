"""Maquina vending: conta premios pelo valor que entra, nao pelo sensor.

Cadastro na maquina: vending_premios premio(s) a cada vending_valor reais.
Toda entrada (Transacao IN, de qualquer origem: moeda, Pix, cartao, app)
soma no saldo da maquina (vending_saldo). A cada vending_valor fechado saem
vending_premios premios, gravados como Transacao OUT - assim relatorios,
cards e fechamentos contam do mesmo jeito que contam a saida do sensor.
O que sobra fica guardado para a proxima entrada (R$ 12 nao conta nada;
depois mais R$ 3 fecha os R$ 15 e conta 1).

O calculo roda num before_flush da sessao, entao nao precisa mexer em cada
lugar que grava uma entrada. Maquina nao vending nao passa por aqui.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.models.models import EventoTipo, Maquina, MetodoPagamento, Transacao


def _centavos(valor) -> int:
    return int(round(float(valor or 0) * 100))


def eh_vending(maquina: Maquina | None) -> bool:
    return bool(
        maquina is not None
        and maquina.maquina_vending
        and (maquina.vending_premios or 0) > 0
        and _centavos(maquina.vending_valor) > 0
    )


def campos_vending(maquina: Maquina) -> dict:
    """Campos para os serializers da maquina (cards / edicao)."""
    return {
        "maquina_vending": bool(maquina.maquina_vending),
        "vending_premios": maquina.vending_premios,
        "vending_valor": maquina.vending_valor,
        "vending_saldo": round(float(maquina.vending_saldo or 0), 2),
    }


def aplicar_vending(
    db_maquina: Maquina,
    maquina_vending: bool | None,
    vending_premios: int | None,
    vending_valor: float | None,
) -> None:
    """Grava a configuracao vending vinda do formulario. None = nao mexe.
    Mudar a regra (ligar, desligar, outra quantidade ou valor) zera o saldo
    acumulado, para nao misturar troco de uma regra com a outra."""
    if maquina_vending is None:
        return
    anterior = (bool(db_maquina.maquina_vending), db_maquina.vending_premios, db_maquina.vending_valor)
    if maquina_vending:
        premios = vending_premios if vending_premios is not None else db_maquina.vending_premios
        valor = vending_valor if vending_valor is not None else db_maquina.vending_valor
        if premios is None or int(premios) <= 0:
            raise HTTPException(status_code=422, detail="Informe a quantidade de premios (maior que zero)")
        if valor is None or _centavos(valor) <= 0:
            raise HTTPException(status_code=422, detail="Informe o valor em reais para sair o premio (maior que zero)")
        db_maquina.vending_premios = int(premios)
        db_maquina.vending_valor = round(float(valor), 2)
    db_maquina.maquina_vending = bool(maquina_vending)
    novo = (bool(db_maquina.maquina_vending), db_maquina.vending_premios, db_maquina.vending_valor)
    if novo != anterior:
        db_maquina.vending_saldo = 0.0


def calcular_premios(saldo_centavos: int, premios: int, valor_centavos: int) -> tuple[int, int]:
    """(premios que saem, saldo que fica) para um saldo acumulado."""
    ciclos = saldo_centavos // valor_centavos if saldo_centavos > 0 else 0
    return ciclos * premios, saldo_centavos - ciclos * valor_centavos


def _eh_entrada(transacao: Transacao) -> bool:
    tipo = transacao.tipo
    valor_tipo = getattr(tipo, "value", tipo)
    return valor_tipo in (EventoTipo.in_flux.value, EventoTipo.in_flux.name) and _centavos(transacao.valor) > 0


def _contar_entrada(session: Session, transacao: Transacao) -> None:
    # Le a regra direto do banco (sem autoflush) - a instancia da sessao pode
    # ter alteracoes pendentes que nao queremos atropelar.
    regra = session.execute(
        text(
            "SELECT maquina_vending, vending_premios, vending_valor FROM maquinas "
            "WHERE id_hardware = :id"
        ),
        {"id": transacao.maquina_id},
    ).first()
    if not regra or not regra[0] or not regra[1] or _centavos(regra[2]) <= 0:
        return
    premios, valor_centavos = int(regra[1]), _centavos(regra[2])

    # UPDATE atomico: trava a linha da maquina ate o commit, entao duas
    # entradas ao mesmo tempo nao perdem troco.
    saldo = session.execute(
        text(
            "UPDATE maquinas SET vending_saldo = COALESCE(vending_saldo, 0) + :v "
            "WHERE id_hardware = :id RETURNING vending_saldo"
        ),
        {"v": _centavos(transacao.valor) / 100, "id": transacao.maquina_id},
    ).scalar()
    saidas, resto = calcular_premios(_centavos(saldo), premios, valor_centavos)
    resto_reais = resto / 100
    if saidas:
        session.execute(
            text("UPDATE maquinas SET vending_saldo = :s WHERE id_hardware = :id"),
            {"s": resto_reais, "id": transacao.maquina_id},
        )
        for _ in range(saidas):
            session.add(
                Transacao(
                    maquina_id=transacao.maquina_id,
                    tipo=EventoTipo.out_flux,
                    metodo=MetodoPagamento.fisico,
                    valor=0.0,
                    data_hora=transacao.data_hora or datetime.utcnow(),
                )
            )
    # Mantem a instancia da sessao (se carregada) igual ao banco, sem marca-la
    # como alterada.
    for obj in list(session.identity_map.values()):
        if isinstance(obj, Maquina) and obj.id_hardware == transacao.maquina_id:
            set_committed_value(obj, "vending_saldo", resto_reais)


@event.listens_for(Session, "before_flush")
def _vending_before_flush(session: Session, flush_context, instances) -> None:
    entradas = [obj for obj in session.new if isinstance(obj, Transacao) and _eh_entrada(obj)]
    if not entradas:
        return
    with session.no_autoflush:
        for transacao in entradas:
            if transacao.maquina_id:
                _contar_entrada(session, transacao)

"""Diagnostico pontual: mostra as vendas fisico/moeda_nota de uma maquina e
quaisquer comandos (ComandoMaquina) que existam para ela, sem filtro de janela,
para entender por que a correlacao automatica nao encontrou nada."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import SessionLocal
from app.models.models import ComandoMaquina, HistoricoOperacao, Maquina, VendaPagamento

MACHINE_ID = sys.argv[1] if len(sys.argv) > 1 else "1001"

db = SessionLocal()
try:
    maquina = db.query(Maquina).filter(Maquina.id_hardware == MACHINE_ID).first()
    print(f"Maquina {MACHINE_ID}: {'encontrada' if maquina else 'NAO ENCONTRADA'}")

    vendas = (
        db.query(VendaPagamento)
        .filter(
            VendaPagamento.maquina_id == MACHINE_ID,
            VendaPagamento.origem == "fisico",
            VendaPagamento.provider == "fisico",
        )
        .order_by(VendaPagamento.created_at.asc())
        .all()
    )
    print(f"\n{len(vendas)} venda(s) fisico/fisico para esta maquina:")
    for v in vendas:
        print(f"  id={v.id} valor={v.valor_liquido} tipo_pagamento={v.tipo_pagamento} is_teste={v.is_teste} created_at={v.created_at}")

    comandos = (
        db.query(ComandoMaquina)
        .filter(ComandoMaquina.maquina_id == MACHINE_ID)
        .order_by(ComandoMaquina.created_at.asc())
        .all()
    )
    print(f"\n{len(comandos)} comando(s) registrado(s) em ComandoMaquina para esta maquina:")
    for c in comandos:
        print(f"  command_id={c.command_id} tipo={c.tipo} status={c.status} created_at={c.created_at}")

    testes = (
        db.query(HistoricoOperacao)
        .filter(HistoricoOperacao.maquina_id == MACHINE_ID, HistoricoOperacao.categoria == "TESTE")
        .order_by(HistoricoOperacao.created_at.asc())
        .all()
    )
    print(f"\n{len(testes)} credito(s) de teste (HistoricoOperacao categoria=TESTE) para esta maquina:")
    for t in testes:
        print(f"  id={t.id} valor={t.valor} command_id={t.command_id} created_at={t.created_at} descricao={t.descricao}")
finally:
    db.close()

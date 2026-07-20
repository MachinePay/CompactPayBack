"""Encontra vendas 'fisico/moeda_nota' que na verdade sao residuo do pulso de
credito (teste ou pago) liberado para a mesma maquina poucos segundos antes -
o mesmo problema corrigido em tempo real em app/services/mqtt_worker.py
(PHYSICAL_COIN_SUPPRESSION_AFTER_CREDIT), aplicado aqui de forma retroativa.

Por padrao roda em modo leitura (soh lista os candidatos). Use --apply para
de fato marcar os registros encontrados como teste (is_teste=True) e tira-los
do faturamento (conta_faturamento=False, conta_ticket_medio=False). Nenhum
registro e apagado - so marcado corretamente.

Uso:
    python -m scripts.fix_phantom_coin_sales                 # so lista (seguro)
    python -m scripts.fix_phantom_coin_sales --apply          # aplica a correcao
    python -m scripts.fix_phantom_coin_sales --window 15      # janela em segundos (padrao 15)
"""
import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import SessionLocal
from app.models.models import ComandoMaquina, VendaPagamento

CREDIT_COMMAND_TYPES = {"paid", "pulse"}


def find_candidates(db, window_seconds: int):
    candidates = []
    vendas = (
        db.query(VendaPagamento)
        .filter(
            VendaPagamento.origem == "fisico",
            VendaPagamento.provider == "fisico",
            VendaPagamento.tipo_pagamento == "moeda_nota",
            VendaPagamento.is_teste.is_(False),
        )
        .order_by(VendaPagamento.created_at.asc())
        .all()
    )
    for venda in vendas:
        comando = (
            db.query(ComandoMaquina)
            .filter(
                ComandoMaquina.maquina_id == venda.maquina_id,
                ComandoMaquina.tipo.in_(CREDIT_COMMAND_TYPES),
                ComandoMaquina.created_at <= venda.created_at,
                ComandoMaquina.created_at >= venda.created_at - timedelta(seconds=window_seconds),
            )
            .order_by(ComandoMaquina.created_at.desc())
            .first()
        )
        if comando:
            candidates.append((venda, comando))
    return candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Aplica a correcao (padrao: so lista)")
    parser.add_argument("--window", type=int, default=15, help="Janela em segundos para correlacionar (padrao 15)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        candidates = find_candidates(db, args.window)
        if not candidates:
            print("Nenhum candidato encontrado.")
            return

        print(f"{len(candidates)} venda(s) fisica(s) suspeita(s) de serem residuo de credito:\n")
        for venda, comando in candidates:
            gap = (venda.created_at - comando.created_at).total_seconds()
            print(
                f"  venda_id={venda.id} maquina={venda.maquina_id} "
                f"valor=R${venda.valor_liquido:.2f} venda_em={venda.created_at} "
                f"| comando_id={comando.command_id} tipo={comando.tipo} "
                f"comando_em={comando.created_at} (gap={gap:.1f}s)"
            )

        if not args.apply:
            print("\nModo leitura (nada foi alterado). Rode com --apply para corrigir esses registros.")
            return

        for venda, _ in candidates:
            venda.is_teste = True
            venda.conta_faturamento = False
            venda.conta_ticket_medio = False
        db.commit()
        print(f"\n{len(candidates)} registro(s) corrigido(s): marcados como teste e removidos do faturamento.")
    finally:
        db.close()


if __name__ == "__main__":
    main()

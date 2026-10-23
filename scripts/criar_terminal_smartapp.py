"""Gera a chave de uma maquininha Point (banco "mercado_pago_apk") direto no banco.

Mesmo efeito de POST /api/v1/smartapp/terminais, para quando o painel ainda
nao tem tela para isso. Usa DATABASE_URL do ambiente (ou do backend/.env).

    python scripts/criar_terminal_smartapp.py --listar-clientes
    python scripts/criar_terminal_smartapp.py --cliente-id 3 --nome "Point Loja X"

A chave aparece uma unica vez; no banco fica so o hash.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.models import Cliente, Maquina
from app.models.smartapp import TerminalSmartApp
from app.services.smartapp import BANCO_SMARTAPP, gerar_chave, hash_chave


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listar-clientes", action="store_true")
    parser.add_argument("--cliente-id", type=int)
    parser.add_argument("--nome")
    args = parser.parse_args()

    engine = create_engine(os.environ["DATABASE_URL"])
    db = sessionmaker(bind=engine)()
    try:
        if args.listar_clientes:
            for cliente in db.query(Cliente).order_by(Cliente.id).all():
                apk = (
                    db.query(Maquina)
                    .filter(Maquina.cliente_id == cliente.id, Maquina.banco_pagamento == BANCO_SMARTAPP)
                    .count()
                )
                mp = "MP conectado" if cliente.mp_access_token else "sem MP"
                print(f"{cliente.id:>4}  {cliente.nome_empresa}  ({mp}, {apk} maquina(s) Mercado Pago APK)")
            return

        if not args.cliente_id or not args.nome:
            parser.error("informe --cliente-id e --nome (ou --listar-clientes)")
        cliente = db.query(Cliente).filter(Cliente.id == args.cliente_id).first()
        if not cliente:
            sys.exit(f"Cliente {args.cliente_id} nao encontrado")

        # So a tabela nova; nao mexe nas outras.
        TerminalSmartApp.__table__.create(bind=engine, checkfirst=True)

        chave = gerar_chave()
        terminal = TerminalSmartApp(
            cliente_id=cliente.id,
            nome=args.nome.strip(),
            chave_prefixo=chave[:10],
            chave_hash=hash_chave(chave),
            ativo=True,
        )
        db.add(terminal)
        db.commit()
        print(f"Terminal {terminal.id} criado para {cliente.nome_empresa}: {terminal.nome}")
        print(f"CHAVE (guarde agora, nao aparece de novo): {chave}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

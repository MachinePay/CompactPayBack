"""Indices para as consultas que crescem com o numero de maquinas: quase tudo
(faturamento, ranking, horarios de pico, historico) filtra por maquina +
periodo.

Os Index() abaixo se registram nas tabelas ao importar este modulo, entao um
banco novo ja nasce com eles (create_all). Banco que ja existe recebe os
indices por criar_indices_em_segundo_plano(): em PostgreSQL usa
CREATE INDEX CONCURRENTLY, que nao trava gravacao (pagamentos continuam
entrando enquanto o indice e' montado) e roda numa thread para nao atrasar a
subida do servidor.
"""

import logging
import threading

from sqlalchemy import Index, text

from app.models.models import AuditoriaSistema, HistoricoOperacao, Maquina, Transacao, Usuario, VendaPagamento

logger = logging.getLogger(__name__)

INDICES = [
    Index("ix_transacoes_maquina_data", Transacao.maquina_id, Transacao.data_hora),
    Index("ix_historico_operacoes_maquina_data", HistoricoOperacao.maquina_id, HistoricoOperacao.created_at),
    Index("ix_historico_operacoes_created_at", HistoricoOperacao.created_at),
    Index("ix_vendas_pagamentos_maquina_data", VendaPagamento.maquina_id, VendaPagamento.created_at),
    Index("ix_maquinas_cliente_id", Maquina.cliente_id),
    Index("ix_usuarios_cliente_id", Usuario.cliente_id),
    Index("ix_auditoria_sistema_created_at", AuditoriaSistema.created_at),
]

# Numero qualquer, fixo: so uma instancia do backend monta os indices por vez.
_TRAVA_POSTGRES = 7_301_002


def _ddl(indice: Index, concorrente: bool) -> str:
    colunas = ", ".join(coluna.name for coluna in indice.columns)
    modo = "CONCURRENTLY " if concorrente else ""
    return f"CREATE INDEX {modo}IF NOT EXISTS {indice.name} ON {indice.table.name} ({colunas})"


def criar_indices(engine) -> list[str]:
    """Cria os indices que faltam. Devolve os nomes criados (ou refeitos)."""
    criados = []
    if engine.dialect.name != "postgresql":
        with engine.begin() as conexao:
            for indice in INDICES:
                conexao.execute(text(_ddl(indice, concorrente=False)))
        return [indice.name for indice in INDICES]

    # CONCURRENTLY nao roda dentro de transacao: conexao em autocommit.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conexao:
        if not conexao.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _TRAVA_POSTGRES}).scalar():
            logger.info("Indices: outra instancia ja esta criando, pulando")
            return criados
        try:
            for indice in INDICES:
                valido = conexao.execute(
                    text(
                        "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                        "WHERE c.relname = :nome"
                    ),
                    {"nome": indice.name},
                ).scalar()
                if valido:
                    continue
                if valido is False:
                    # Sobra de uma criacao CONCURRENTLY interrompida: refaz.
                    conexao.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {indice.name}"))
                logger.info("Indices: criando %s", indice.name)
                conexao.execute(text(_ddl(indice, concorrente=True)))
                criados.append(indice.name)
        finally:
            conexao.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _TRAVA_POSTGRES})
    return criados


def _rodar(engine) -> None:
    try:
        criados = criar_indices(engine)
        if criados:
            logger.info("Indices criados: %s", ", ".join(criados))
    except Exception:
        logger.exception("Indices: falha ao criar (o sistema continua funcionando, so sem o ganho)")


def criar_indices_em_segundo_plano(engine) -> threading.Thread:
    thread = threading.Thread(target=_rodar, args=(engine,), daemon=True, name="criar-indices")
    thread.start()
    return thread

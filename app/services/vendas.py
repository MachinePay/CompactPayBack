from datetime import datetime

from app.models.models import VendaPagamento


def registrar_venda_pagamento(
    db,
    *,
    maquina_id: str,
    valor: float,
    origem: str,
    transacao_id: int | None = None,
    historico_id: int | None = None,
    provider: str | None = None,
    provider_payment_id: str | None = None,
    tipo_pagamento: str | None = None,
    bandeira_cartao: str | None = None,
    banco: str | None = None,
    taxa: float | None = None,
    status_pulso: str | None = None,
    command_id: str | None = None,
    conta_faturamento: bool = True,
    conta_ticket_medio: bool = True,
    is_teste: bool = False,
    is_manual: bool = False,
    created_at: datetime | None = None,
) -> VendaPagamento:
    bruto = float(valor or 0)
    taxa_valor = float(taxa) if taxa is not None else None
    venda = VendaPagamento(
        maquina_id=maquina_id,
        transacao_id=transacao_id,
        historico_id=historico_id,
        origem=origem,
        provider=provider,
        provider_payment_id=provider_payment_id,
        tipo_pagamento=tipo_pagamento,
        bandeira_cartao=bandeira_cartao,
        banco=banco,
        valor_bruto=bruto,
        taxa=taxa_valor,
        valor_liquido=bruto - (taxa_valor or 0),
        status_pulso=status_pulso,
        command_id=command_id,
        conta_faturamento=conta_faturamento,
        conta_ticket_medio=conta_ticket_medio,
        is_teste=is_teste,
        is_manual=is_manual,
        created_at=created_at or datetime.utcnow(),
    )
    db.add(venda)
    return venda

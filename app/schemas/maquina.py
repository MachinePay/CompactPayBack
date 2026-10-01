from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class MaquinaCreate(BaseModel):
    id_hardware: Optional[str] = None
    nome: str
    cliente_id: Optional[int] = None
    localizacao: Optional[str] = None
    banco_pagamento: Optional[str] = "mercado_pago"
    # Obrigatorio quando banco_pagamento == "sumup" - qual reader (maquininha)
    # ja pareado na conta SumUp do cliente fica vinculado a esta maquina.
    sumup_reader_id: Optional[str] = None


class MaquinaUpdate(BaseModel):
    nome: str
    cliente_id: Optional[int] = None
    localizacao: Optional[str] = None
    banco_pagamento: Optional[str] = None
    sumup_reader_id: Optional[str] = None


class MaquinaOut(BaseModel):
    id_hardware: str
    cliente_id: Optional[int] = None
    cliente_nome: Optional[str] = None
    nome: Optional[str] = None
    localizacao: Optional[str] = None
    banco_pagamento: Optional[str] = None
    mp_store_id: Optional[str] = None
    mp_store_external_id: Optional[str] = None
    mp_pos_id: Optional[str] = None
    mp_pos_external_id: Optional[str] = None
    mp_qr_image: Optional[str] = None
    sumup_reader_id: Optional[str] = None
    firmware_version: Optional[str] = None
    firmware_target_version: Optional[str] = None
    firmware_updated_at: Optional[datetime] = None
    firmware_update_status: Optional[str] = None
    firmware_update_command_id: Optional[str] = None
    firmware_update_url: Optional[str] = None
    firmware_update_requested_at: Optional[datetime] = None
    firmware_update_started_at: Optional[datetime] = None
    firmware_update_finished_at: Optional[datetime] = None
    ultimo_sinal: Optional[datetime] = None
    wifi_rssi: Optional[int] = None
    wifi_quality: Optional[int] = None
    ultimo_pagamento_em: Optional[datetime] = None
    ultimo_teste_em: Optional[datetime] = None
    ultima_saida_em: Optional[datetime] = None
    ultima_atividade_em: Optional[datetime] = None
    status_online: bool = False
    status_operacional: str = "offline"
    faturamento: float = 0.0
    quantidade_saidas: int = 0
    ignorar_saida_pos_credito: bool = True
    wifi_hard_reset_ms: Optional[int] = None
    wifi_full_restart_ms: Optional[int] = None
    pulse_coin: Optional[str] = None
    pulse_out: Optional[str] = None
    pulse_credit: Optional[str] = None
    pulse_value: Optional[str] = None
    pulse_quantity: Optional[str] = None
    coin_debounce_us: Optional[str] = None
    coin_release_ms: Optional[str] = None

    class Config:
        from_attributes = True

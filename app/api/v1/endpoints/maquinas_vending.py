"""Regra vending da maquina editada pelo cliente dono (ou pelo admin).

O PUT /maquinas/{id} (maquinas.py) continua so do admin. Aqui o cliente liga
ou desliga a maquina vending e cadastra quantos premios saem a cada quantos
reais (calculo em services/vending.py). Maquina de outro cliente da 404.
"""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.escopo import obter_maquina_do_usuario
from app.db.session import SessionLocal
from app.services.auditoria import registrar_auditoria
from app.services.vending import aplicar_vending, campos_vending

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class VendingUpdate(BaseModel):
    maquina_vending: bool
    vending_premios: Optional[int] = None
    vending_valor: Optional[float] = None


@router.patch("/maquinas/{machine_id}/vending")
def atualizar_vending_da_maquina(
    machine_id: str,
    dados: VendingUpdate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    maquina = obter_maquina_do_usuario(db, user, machine_id)
    anterior = campos_vending(maquina)
    aplicar_vending(maquina, dados.maquina_vending, dados.vending_premios, dados.vending_valor)
    novo = campos_vending(maquina)
    if novo != anterior:
        registrar_auditoria(
            db,
            user,
            acao="MAQUINA_VENDING_ATUALIZADA",
            entidade_tipo="maquina",
            entidade_id=machine_id,
            descricao=(
                f"Vending {anterior['maquina_vending']}->{novo['maquina_vending']} "
                f"premios={anterior['vending_premios']}->{novo['vending_premios']} "
                f"valor={anterior['vending_valor']}->{novo['vending_valor']}"
            ),
        )
    db.commit()
    return {"id_hardware": maquina.id_hardware, **campos_vending(maquina)}

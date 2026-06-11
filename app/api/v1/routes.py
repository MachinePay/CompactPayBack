from fastapi import APIRouter

from app.api.v1.endpoints import (
    auditoria,
    auth,
    clientes,
    dashboard,
    maquinas,
    mercado_pago,
    pagamentos,
    produtos,
    relatorios,
    usuarios,
)

router = APIRouter()

router.include_router(auth.router)
router.include_router(usuarios.router)
router.include_router(mercado_pago.router)
router.include_router(auditoria.router)
router.include_router(clientes.router)
router.include_router(maquinas.router)
router.include_router(produtos.router)
router.include_router(relatorios.router)
router.include_router(pagamentos.router)
router.include_router(dashboard.router)

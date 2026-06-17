from fastapi import APIRouter

from app.api.v1.endpoints import (
    auditoria,
    auth,
    clientes,
    dashboard,
    firmware_versions,
    health,
    maquinas,
    maquinas_operacoes,
    maquinas_relatorios,
    mercado_pago,
    pagamentos,
    pagamentos_escuta,
    produtos,
    relatorios,
    usuarios,
)

router = APIRouter()

router.include_router(auth.router)
router.include_router(health.router)
router.include_router(usuarios.router)
router.include_router(mercado_pago.router)
router.include_router(auditoria.router)
router.include_router(clientes.router)
router.include_router(firmware_versions.router)
router.include_router(maquinas.router)
router.include_router(maquinas_operacoes.router)
router.include_router(maquinas_relatorios.router)
router.include_router(produtos.router)
router.include_router(relatorios.router)
router.include_router(pagamentos.router)
router.include_router(pagamentos_escuta.router)
router.include_router(dashboard.router)

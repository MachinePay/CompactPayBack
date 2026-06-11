from fastapi import APIRouter

from app.api.v1.endpoints import (
    auditoria,
    auth,
    clientes,
    dashboard,
    maquinas,
    maquinas_operacoes,
    mercado_pago,
    pagamentos,
    pagamentos_escuta,
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
router.include_router(maquinas_operacoes.router)
router.include_router(produtos.router)
router.include_router(relatorios.router)
router.include_router(pagamentos.router)
router.include_router(pagamentos_escuta.router)
router.include_router(dashboard.router)

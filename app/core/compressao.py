"""Compressao GZip das respostas, menos o download de firmware: a placa
espera o .bin exatamente como e' (OTA), entao essa rota nunca e' compactada.
Respostas pequenas tambem nao (minimum_size)."""

from starlette.middleware.gzip import GZipMiddleware

ROTAS_SEM_COMPRESSAO = ("/api/v1/firmware-files/",)


class GZipExcetoFirmware:
    def __init__(self, app, minimum_size: int = 1000):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith(ROTAS_SEM_COMPRESSAO):
            await self.app(scope, receive, send)
            return
        await self.gzip(scope, receive, send)

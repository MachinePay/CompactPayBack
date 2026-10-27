"""Em producao ou nao, e ajustes que dependem disso.

Producao = APP_ENV=production, ou rodando no Render (que define RENDER=true
em todo servico). APP_ENV=development forca o modo de desenvolvimento.
"""

import os


def em_producao() -> bool:
    app_env = os.getenv("APP_ENV", "").strip().lower()
    if app_env:
        return app_env in {"production", "producao", "prod"}
    return os.getenv("RENDER", "").strip().lower() == "true"


def docs_da_api_visiveis() -> bool:
    """/docs e /openapi.json: so fora de producao, ou com EXPOSE_API_DOCS=true."""
    if os.getenv("EXPOSE_API_DOCS", "").strip().lower() == "true":
        return True
    return not em_producao()


ORIGENS_DESENVOLVIMENTO = ["http://localhost:5173", "http://127.0.0.1:5173"]

from app.models.models import AuditoriaSistema


def get_user_email(user) -> str:
    token_data, _, _ = user
    return getattr(token_data, "email", "desconhecido") or "desconhecido"


def registrar_auditoria(
    db,
    user,
    acao: str,
    entidade_tipo: str,
    entidade_id: str | int | None,
    descricao: str,
) -> AuditoriaSistema:
    auditoria = AuditoriaSistema(
        entidade_tipo=entidade_tipo,
        entidade_id=str(entidade_id) if entidade_id is not None else None,
        acao=acao,
        descricao=descricao,
        executado_por_email=get_user_email(user),
    )
    db.add(auditoria)
    return auditoria

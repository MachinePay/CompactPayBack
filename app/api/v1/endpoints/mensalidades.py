"""Mensalidade por cliente (tela de Usuarios, admin) e leitura pelo GrupoGK.

Cada cliente tem um valor padrao por maquina (R$ 40 se nunca foi editado) e,
opcionalmente, faixas com outro valor para N maquinas. As maquinas que nao
cabem em nenhuma faixa sao cobradas pelo valor padrao:

    total = soma(faixa.valor * faixa.quantidade) + valor_padrao * maquinas_no_padrao
    maquinas_no_padrao = max(0, maquinas_do_cliente - soma(faixa.quantidade))

- GET /mensalidades e PUT /mensalidades/{cliente_id}: admin logado.
- GET /integracao/grupogk/mensalidades: o GrupoGK puxa tudo com o header
  X-Api-Key igual a variavel de ambiente GRUPOGK_API_KEY (sem ela, desligado).
"""
import hmac
import os

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.mensalidade import VALOR_PADRAO_MENSALIDADE, MensalidadeCliente, MensalidadeFaixa
from app.models.models import Cliente, Maquina, Usuario

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class FaixaEntrada(BaseModel):
    valor: float = Field(ge=0)
    quantidade_maquinas: int = Field(ge=1)
    descricao: str | None = None


class MensalidadeEntrada(BaseModel):
    valor_padrao: float = Field(ge=0)
    faixas: list[FaixaEntrada] = []


def _exigir_admin(user):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin")


def _montar(db: Session, cliente_ids: list[int] | None = None) -> list[dict]:
    q_clientes = db.query(Cliente.id, Cliente.nome_empresa, Cliente.email_contato)
    if cliente_ids is not None:
        q_clientes = q_clientes.filter(Cliente.id.in_(cliente_ids))
    clientes = q_clientes.order_by(Cliente.id).all()

    maquinas = dict(
        db.query(Maquina.cliente_id, func.count(Maquina.id_hardware))
        .filter(Maquina.cliente_id.isnot(None))
        .group_by(Maquina.cliente_id)
        .all()
    )
    padroes = dict(db.query(MensalidadeCliente.cliente_id, MensalidadeCliente.valor_padrao).all())
    faixas: dict[int, list[dict]] = {}
    for f in db.query(MensalidadeFaixa).order_by(MensalidadeFaixa.id).all():
        faixas.setdefault(f.cliente_id, []).append(
            {
                "id": f.id,
                "valor": f.valor,
                "quantidade_maquinas": f.quantidade_maquinas,
                "descricao": f.descricao,
            }
        )

    resultado = []
    for cliente_id, nome_empresa, email_contato in clientes:
        valor_padrao = padroes.get(cliente_id, VALOR_PADRAO_MENSALIDADE)
        lista = faixas.get(cliente_id, [])
        total_maquinas = int(maquinas.get(cliente_id, 0))
        maquinas_em_faixas = sum(f["quantidade_maquinas"] for f in lista)
        maquinas_padrao = max(0, total_maquinas - maquinas_em_faixas)
        total = sum(f["valor"] * f["quantidade_maquinas"] for f in lista) + valor_padrao * maquinas_padrao
        resultado.append(
            {
                "cliente_id": cliente_id,
                "nome_empresa": nome_empresa,
                "email_contato": email_contato,
                "valor_padrao": valor_padrao,
                "padrao_editado": cliente_id in padroes,
                "faixas": lista,
                "maquinas_total": total_maquinas,
                "maquinas_em_faixas": maquinas_em_faixas,
                "maquinas_padrao": maquinas_padrao,
                # Faixas com mais maquinas do que o cliente tem hoje.
                "maquinas_excedentes": max(0, maquinas_em_faixas - total_maquinas),
                "total_mensal": round(total, 2),
            }
        )
    return resultado


@router.get("/mensalidades")
def listar_mensalidades(db: Session = Depends(get_db), user=Depends(get_current_user)):
    _exigir_admin(user)
    return _montar(db)


@router.put("/mensalidades/{cliente_id}")
def salvar_mensalidade(
    cliente_id: int,
    dados: MensalidadeEntrada,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _exigir_admin(user)
    if not db.query(Cliente.id).filter(Cliente.id == cliente_id).first():
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")

    padrao = db.query(MensalidadeCliente).filter(MensalidadeCliente.cliente_id == cliente_id).first()
    if padrao is None:
        padrao = MensalidadeCliente(cliente_id=cliente_id)
        db.add(padrao)
    padrao.valor_padrao = dados.valor_padrao

    db.query(MensalidadeFaixa).filter(MensalidadeFaixa.cliente_id == cliente_id).delete()
    for f in dados.faixas:
        db.add(
            MensalidadeFaixa(
                cliente_id=cliente_id,
                valor=f.valor,
                quantidade_maquinas=f.quantidade_maquinas,
                descricao=(f.descricao or "").strip() or None,
            )
        )
    db.commit()
    return _montar(db, [cliente_id])[0]


@router.get("/integracao/grupogk/mensalidades")
def mensalidades_grupogk(
    x_api_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    esperado = os.getenv("GRUPOGK_API_KEY", "")
    if not esperado:
        raise HTTPException(status_code=503, detail="Integracao GrupoGK desligada (GRUPOGK_API_KEY)")
    if not x_api_key or not hmac.compare_digest(x_api_key, esperado):
        raise HTTPException(status_code=401, detail="Chave invalida")

    mensalidades = _montar(db)
    usuarios: dict[int, list[dict]] = {}
    for u in db.query(Usuario.cliente_id, Usuario.nome, Usuario.email).filter(Usuario.cliente_id.isnot(None)).all():
        usuarios.setdefault(u.cliente_id, []).append({"nome": u.nome, "email": u.email})
    for item in mensalidades:
        item["usuarios"] = usuarios.get(item["cliente_id"], [])
    return {"valor_padrao_sistema": VALOR_PADRAO_MENSALIDADE, "clientes": mensalidades}

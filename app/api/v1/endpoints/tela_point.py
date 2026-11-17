"""Rotas do banco "mercado_pago_point" (tela touch ESP32 + Point em modo integrado).

- /tela/point/... e /tela/telas...: painel (login JWT) ve as Points da conta,
  coloca uma Point em modo integrado e cadastra/revoga a tela de cada maquina.
- /tela/config e /tela/cobrancas...: a propria tela, header X-Tela-Key.

Logica em app/services/tela_point.py. Nada aqui altera rotas/fluxos existentes;
o pagamento aprovado segue pelo webhook de order que ja existe.
"""
import os
import tempfile

from fastapi import APIRouter, Depends, File, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.core.escopo import eh_admin, obter_maquina_do_usuario
from app.db.session import SessionLocal
from app.models.models import Cliente
from app.models.produto import Produto
from app.models.tela_point import TelaPoint, VideoTela
from app.services import tela_point as svc
from app.services import tela_video
from app.services.auditoria import registrar_auditoria
from app.services.smartapp import maquina_online, verificar_placa_online

router = APIRouter(prefix="/tela", tags=["tela-point"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_tela(
    x_tela_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> TelaPoint:
    return svc.autenticar_tela(db, x_tela_key)


class TelaCreate(BaseModel):
    maquina_id: str = Field(min_length=1)
    nome: str = Field(min_length=1, max_length=80)
    point_terminal_id: str = Field(min_length=3, max_length=120)


class CobrancaCreate(BaseModel):
    produto_id: int


def _serialize_tela(tela: TelaPoint) -> dict:
    return {
        "id": tela.id,
        "cliente_id": tela.cliente_id,
        "maquina_id": tela.maquina_id,
        "nome": tela.nome,
        "point_terminal_id": tela.point_terminal_id,
        "chave_prefixo": tela.chave_prefixo,
        "ativo": tela.ativo,
        "created_at": tela.created_at,
        "ultimo_uso_em": tela.ultimo_uso_em,
    }


def _cliente_do_painel(db: Session, user, cliente_id: int | None) -> int:
    cliente_id = cliente_id if eh_admin(user) else user[2]
    if cliente_id is None:
        raise HTTPException(status_code=422, detail="Informe o cliente")
    if not db.query(Cliente).filter(Cliente.id == cliente_id).first():
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    return cliente_id


# ---- Painel: Points da conta ----------------------------------------------

@router.get("/point/terminais")
def listar_points(
    cliente_id: int | None = None,
    maquina_id: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Com maquina_id, marca em "neste_caixa" a Point vinculada ao caixa dessa maquina."""
    maquina = obter_maquina_do_usuario(db, user, maquina_id) if maquina_id else None
    if maquina and maquina.cliente_id:
        cliente_id = maquina.cliente_id
    else:
        cliente_id = _cliente_do_painel(db, user, cliente_id)
    points = svc.listar_terminais_point(db, cliente_id)
    for point in points:
        point["neste_caixa"] = bool(maquina and svc.point_no_caixa(point, maquina))
    return points


# ---- Painel: caixa Mercado Pago da maquina ----------------------------------------

def _maquina_de_tela(db: Session, user, machine_id: str):
    maquina = obter_maquina_do_usuario(db, user, machine_id)
    if maquina.banco_pagamento != svc.BANCO_TELA_POINT:
        raise HTTPException(status_code=422, detail="A maquina precisa estar com o banco Mercado Pago Point (tela)")
    return maquina


@router.get("/maquinas/{machine_id}/caixa")
def caixa_da_maquina(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    maquina = _maquina_de_tela(db, user, machine_id)
    return {"maquina_id": maquina.id_hardware, "caixa": svc.caixa_da_maquina(maquina)}


@router.post("/maquinas/{machine_id}/caixa")
def criar_caixa(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    maquina = _maquina_de_tela(db, user, machine_id)
    tinha = svc.caixa_da_maquina(maquina) is not None
    caixa = svc.criar_caixa(db, maquina)
    if not tinha:
        registrar_auditoria(
            db,
            user,
            acao="TELA_POINT_CAIXA_CRIADO",
            entidade_tipo="maquina",
            entidade_id=maquina.id_hardware,
            descricao=f"Caixa Mercado Pago criado para a maquina de tela pos_id={caixa['pos_id']}",
        )
        db.commit()
    return {"maquina_id": maquina.id_hardware, "caixa": caixa}


# ---- Painel: video da tela ----------------------------------------------------

UPLOAD_MAX_BYTES = 300 * 1024 * 1024


def _video_da_maquina(db: Session, machine_id: str) -> VideoTela | None:
    return db.query(VideoTela).filter(VideoTela.maquina_id == machine_id).order_by(VideoTela.id.desc()).first()


def _video_info(video: VideoTela | None) -> dict | None:
    if not video:
        return None
    return {
        "versao": video.versao,
        "tamanho": video.tamanho,
        "quadros": video.quadros,
        "fps": video.fps,
        "duracao_s": round(video.quadros / max(video.fps, 1), 1),
        "nome_original": video.nome_original,
        "created_at": video.created_at,
    }


@router.get("/maquinas/{machine_id}/video")
def ver_video(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    maquina = _maquina_de_tela(db, user, machine_id)
    return {"maquina_id": maquina.id_hardware, "video": _video_info(_video_da_maquina(db, maquina.id_hardware))}


@router.post("/maquinas/{machine_id}/video")
def enviar_video(
    machine_id: str,
    arquivo: UploadFile = File(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Recebe o video (MP4, MOV...), converte para o formato da tela e troca o
    video da maquina. A tela baixa sozinha na proxima verificacao."""
    maquina = _maquina_de_tela(db, user, machine_id)
    with tempfile.TemporaryDirectory() as pasta:
        entrada = os.path.join(pasta, "entrada")
        recebidos = 0
        with open(entrada, "wb") as destino:
            while True:
                bloco = arquivo.file.read(1024 * 1024)
                if not bloco:
                    break
                recebidos += len(bloco)
                if recebidos > UPLOAD_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="Video muito grande (maximo 300 MB)")
                destino.write(bloco)
        if recebidos == 0:
            raise HTTPException(status_code=422, detail="Arquivo de video vazio")
        dados, quadros = tela_video.converter_para_tela(entrada)

    db.query(VideoTela).filter(VideoTela.maquina_id == maquina.id_hardware).delete()
    video = VideoTela(
        cliente_id=maquina.cliente_id,
        maquina_id=maquina.id_hardware,
        nome_original=(arquivo.filename or "")[:120] or None,
        versao=tela_video.versao_do_video(dados),
        tamanho=len(dados),
        quadros=quadros,
        fps=tela_video.FPS,
        arquivo=dados,
    )
    db.add(video)
    db.flush()
    registrar_auditoria(
        db,
        user,
        acao="TELA_VIDEO_ENVIADO",
        entidade_tipo="maquina",
        entidade_id=maquina.id_hardware,
        descricao=(
            f"Video da tela trocado arquivo={video.nome_original} versao={video.versao} "
            f"tamanho={video.tamanho} quadros={quadros}"
        ),
    )
    db.commit()
    db.refresh(video)
    return {"maquina_id": maquina.id_hardware, "video": _video_info(video)}


@router.delete("/maquinas/{machine_id}/video")
def remover_video(machine_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    maquina = _maquina_de_tela(db, user, machine_id)
    apagados = db.query(VideoTela).filter(VideoTela.maquina_id == maquina.id_hardware).delete()
    if apagados:
        registrar_auditoria(
            db,
            user,
            acao="TELA_VIDEO_REMOVIDO",
            entidade_tipo="maquina",
            entidade_id=maquina.id_hardware,
            descricao="Video da tela removido",
        )
    db.commit()
    return {"maquina_id": maquina.id_hardware, "video": None}


@router.post("/point/terminais/{terminal_id}/modo-integrado")
def ativar_modo_integrado(
    terminal_id: str,
    cliente_id: int | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    cliente_id = _cliente_do_painel(db, user, cliente_id)
    resultado = svc.ativar_modo_pdv(db, cliente_id, terminal_id)
    registrar_auditoria(
        db,
        user,
        acao="POINT_MODO_INTEGRADO",
        entidade_tipo="point_terminal",
        entidade_id=terminal_id,
        descricao=f"Point {terminal_id} colocada em modo integrado (PDV) cliente_id={cliente_id}",
    )
    db.commit()
    return {"ok": True, "terminal_id": terminal_id, "mercado_pago": resultado}


# ---- Painel: telas ---------------------------------------------------------

@router.post("/telas")
def criar_tela(dados: TelaCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    maquina = obter_maquina_do_usuario(db, user, dados.maquina_id.strip())
    if maquina.banco_pagamento != svc.BANCO_TELA_POINT:
        raise HTTPException(status_code=422, detail="A maquina precisa estar com o banco Mercado Pago Point (tela)")
    if not maquina.cliente_id:
        raise HTTPException(status_code=422, detail="Maquina sem cliente")
    point_terminal_id = dados.point_terminal_id.strip()
    svc.validar_point_livre(db, maquina, point_terminal_id)

    # Uma tela ativa por maquina: cadastrar de novo troca a chave (tela perdida/trocada).
    anteriores = (
        db.query(TelaPoint)
        .filter(TelaPoint.maquina_id == maquina.id_hardware, TelaPoint.ativo.is_(True))
        .all()
    )
    for anterior in anteriores:
        anterior.ativo = False

    chave = svc.gerar_chave()
    tela = TelaPoint(
        cliente_id=maquina.cliente_id,
        maquina_id=maquina.id_hardware,
        nome=dados.nome.strip(),
        point_terminal_id=point_terminal_id,
        chave_prefixo=chave[:12],
        chave_hash=svc.hash_chave(chave),
        ativo=True,
    )
    db.add(tela)
    db.flush()
    registrar_auditoria(
        db,
        user,
        acao="TELA_POINT_CRIADA",
        entidade_tipo="tela_point",
        entidade_id=tela.id,
        descricao=(
            f"Tela Point criada maquina={maquina.id_hardware} point={tela.point_terminal_id} "
            f"nome={tela.nome} telas_revogadas={len(anteriores)}"
        ),
    )
    db.commit()
    db.refresh(tela)
    # A chave so aparece aqui; no banco fica so o hash.
    return {**_serialize_tela(tela), "chave": chave}


@router.get("/telas")
def listar_telas(db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = db.query(TelaPoint)
    if not eh_admin(user):
        query = query.filter(TelaPoint.cliente_id == user[2])
    return [_serialize_tela(t) for t in query.order_by(TelaPoint.id).all()]


@router.delete("/telas/{tela_id}")
def revogar_tela(tela_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    query = db.query(TelaPoint).filter(TelaPoint.id == tela_id)
    if not eh_admin(user):
        query = query.filter(TelaPoint.cliente_id == user[2])
    tela = query.first()
    if not tela:
        raise HTTPException(status_code=404, detail="Tela nao encontrada")
    tela.ativo = False
    registrar_auditoria(
        db,
        user,
        acao="TELA_POINT_REVOGADA",
        entidade_tipo="tela_point",
        entidade_id=tela.id,
        descricao=f"Tela Point revogada maquina={tela.maquina_id} nome={tela.nome}",
    )
    db.commit()
    return _serialize_tela(tela)


# ---- A propria tela --------------------------------------------------------

@router.get("/config")
def config_da_tela(db: Session = Depends(get_db), tela=Depends(get_tela)):
    maquina = svc.maquina_da_tela(db, tela)
    produtos = db.query(Produto).filter(Produto.maquina_id == maquina.id_hardware).order_by(Produto.valor).all()
    return {
        "maquina_id": maquina.id_hardware,
        "nome": maquina.nome_local or maquina.id_hardware,
        "online": maquina_online(maquina),
        "produtos": [{"id": p.id, "nome": p.nome, "valor": p.valor} for p in produtos if p.valor and p.valor > 0],
        # A tela compara "versao" com a do video que ja tem e baixa em /tela/video se mudou.
        "video": _video_resumo(_video_da_maquina(db, maquina.id_hardware)),
    }


def _video_resumo(video: VideoTela | None) -> dict | None:
    if not video:
        return None
    return {"versao": video.versao, "tamanho": video.tamanho, "fps": video.fps}


@router.get("/video")
def baixar_video(db: Session = Depends(get_db), tela=Depends(get_tela)):
    maquina = svc.maquina_da_tela(db, tela)
    video = _video_da_maquina(db, maquina.id_hardware)
    if not video:
        raise HTTPException(status_code=404, detail="Esta maquina nao tem video")
    return Response(
        content=bytes(video.arquivo),
        media_type="application/octet-stream",
        headers={"X-Video-Versao": video.versao},
    )


@router.post("/cobrancas")
def criar_cobranca(dados: CobrancaCreate, db: Session = Depends(get_db), tela=Depends(get_tela)):
    maquina = svc.maquina_da_tela(db, tela)
    produto = svc.produto_da_maquina(db, maquina, dados.produto_id)
    # Nao cobra se a placa nao responder: o cliente pagaria e nao receberia o credito.
    if not verificar_placa_online(db, maquina):
        raise HTTPException(status_code=409, detail="Maquina indisponivel no momento")
    return svc.criar_cobranca(db, tela, maquina, produto)


@router.get("/cobrancas/{order_id}")
def consultar_cobranca(order_id: str, db: Session = Depends(get_db), tela=Depends(get_tela)):
    maquina = svc.maquina_da_tela(db, tela)
    return svc.consultar_cobranca(db, tela, maquina, order_id)


@router.post("/cobrancas/{order_id}/cancelar")
def cancelar_cobranca(order_id: str, db: Session = Depends(get_db), tela=Depends(get_tela)):
    maquina = svc.maquina_da_tela(db, tela)
    return svc.cancelar_cobranca(db, tela, maquina, order_id)

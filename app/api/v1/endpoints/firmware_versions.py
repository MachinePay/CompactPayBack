from datetime import datetime
from pathlib import Path
from typing import List
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import get_current_user
from app.db.session import SessionLocal
from app.models.models import FirmwareVersion
from app.schemas.firmware import FirmwareVersionCreate, FirmwareVersionOut, FirmwareVersionUpdate
from app.services.auditoria import registrar_auditoria

router = APIRouter()

MAX_FIRMWARE_SIZE_BYTES = 16 * 1024 * 1024


def _firmware_upload_dir() -> Path:
    upload_dir = Path(settings.FIRMWARE_UPLOAD_DIR).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


def _firmware_file_path(filename: str) -> Path:
    upload_dir = _firmware_upload_dir()
    file_path = (upload_dir / Path(filename).name).resolve()
    if upload_dir not in file_path.parents:
        raise HTTPException(status_code=400, detail="Nome de arquivo invalido")
    return file_path


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _require_admin(user):
    _, role, _ = user
    if role != "admin":
        raise HTTPException(status_code=403, detail="Apenas admin pode gerenciar firmwares")


@router.get("/firmware-files/{filename}", name="download_firmware_file")
def download_firmware_file(filename: str):
    file_path = _firmware_file_path(filename)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo de firmware nao encontrado")
    return FileResponse(
        file_path,
        media_type="application/octet-stream",
        filename=file_path.name,
    )


@router.get("/firmware-versions", response_model=List[FirmwareVersionOut])
def listar_firmwares(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _require_admin(user)
    query = db.query(FirmwareVersion)
    if not include_inactive:
        query = query.filter(FirmwareVersion.ativo.is_(True))
    return query.order_by(FirmwareVersion.updated_at.desc(), FirmwareVersion.id.desc()).all()


@router.post("/firmware-versions", response_model=FirmwareVersionOut)
def criar_firmware(
    payload: FirmwareVersionCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _require_admin(user)
    now = datetime.utcnow()
    firmware = FirmwareVersion(
        nome=payload.nome.strip(),
        url_bin=str(payload.url_bin),
        observacao=(payload.observacao or "").strip() or None,
        ativo=payload.ativo,
        created_at=now,
        updated_at=now,
    )
    if not firmware.nome:
        raise HTTPException(status_code=422, detail="Informe o nome da versao")
    db.add(firmware)
    db.flush()
    registrar_auditoria(
        db,
        user,
        acao="FIRMWARE_CRIADO",
        entidade_tipo="firmware",
        entidade_id=firmware.id,
        descricao=f"Firmware cadastrado nome={firmware.nome}",
    )
    db.commit()
    db.refresh(firmware)
    return firmware


@router.post("/firmware-versions/upload", response_model=FirmwareVersionOut)
def criar_firmware_upload(
    request: Request,
    nome: str = Form(...),
    observacao: str | None = Form(None),
    ativo: bool = Form(True),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _require_admin(user)
    original_name = Path(file.filename or "").name
    if not original_name.lower().endswith(".bin"):
        raise HTTPException(status_code=422, detail="Envie um arquivo .bin")

    safe_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:10]}-{original_name}"
    file_path = _firmware_file_path(safe_name)
    bytes_written = 0
    try:
        with file_path.open("wb") as output:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > MAX_FIRMWARE_SIZE_BYTES:
                    output.close()
                    file_path.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="Arquivo muito grande. Limite de 16 MB.")
                output.write(chunk)
    finally:
        file.file.close()

    if bytes_written == 0:
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="Arquivo vazio")

    if settings.BACKEND_PUBLIC_URL:
        file_url = f"{settings.BACKEND_PUBLIC_URL.rstrip('/')}/api/v1/firmware-files/{safe_name}"
    else:
        file_url = str(request.url_for("download_firmware_file", filename=safe_name))
    now = datetime.utcnow()
    firmware = FirmwareVersion(
        nome=nome.strip(),
        url_bin=file_url,
        observacao=(observacao or "").strip() or None,
        ativo=ativo,
        created_at=now,
        updated_at=now,
    )
    if not firmware.nome:
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="Informe o nome da versao")

    db.add(firmware)
    db.flush()
    registrar_auditoria(
        db,
        user,
        acao="FIRMWARE_CRIADO",
        entidade_tipo="firmware",
        entidade_id=firmware.id,
        descricao=f"Firmware cadastrado por upload nome={firmware.nome} arquivo={safe_name}",
    )
    db.commit()
    db.refresh(firmware)
    return firmware


@router.put("/firmware-versions/{firmware_id}", response_model=FirmwareVersionOut)
def atualizar_firmware(
    firmware_id: int,
    payload: FirmwareVersionUpdate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _require_admin(user)
    firmware = db.query(FirmwareVersion).filter(FirmwareVersion.id == firmware_id).first()
    if not firmware:
        raise HTTPException(status_code=404, detail="Firmware nao encontrado")

    if payload.nome is not None:
        firmware.nome = payload.nome.strip()
        if not firmware.nome:
            raise HTTPException(status_code=422, detail="Informe o nome da versao")
    if payload.url_bin is not None:
        firmware.url_bin = str(payload.url_bin)
    if payload.observacao is not None:
        firmware.observacao = payload.observacao.strip() or None
    if payload.ativo is not None:
        firmware.ativo = payload.ativo
    firmware.updated_at = datetime.utcnow()

    registrar_auditoria(
        db,
        user,
        acao="FIRMWARE_ATUALIZADO",
        entidade_tipo="firmware",
        entidade_id=firmware.id,
        descricao=f"Firmware atualizado nome={firmware.nome} ativo={firmware.ativo}",
    )
    db.commit()
    db.refresh(firmware)
    return firmware


@router.delete("/firmware-versions/{firmware_id}")
def remover_firmware(
    firmware_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _require_admin(user)
    firmware = db.query(FirmwareVersion).filter(FirmwareVersion.id == firmware_id).first()
    if not firmware:
        raise HTTPException(status_code=404, detail="Firmware nao encontrado")
    firmware.ativo = False
    firmware.updated_at = datetime.utcnow()
    registrar_auditoria(
        db,
        user,
        acao="FIRMWARE_DESATIVADO",
        entidade_tipo="firmware",
        entidade_id=firmware.id,
        descricao=f"Firmware desativado nome={firmware.nome}",
    )
    db.commit()
    return {"ok": True}

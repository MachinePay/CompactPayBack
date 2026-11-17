"""Video da tela touch (banco "mercado_pago_point").

O painel manda um video comum (MP4, MOV...). Aqui ele vira MJPEG 1024x600 a
20 fps - o formato que o JPEG por hardware do ESP32-P4 toca - e precisa caber
na memoria interna da tela (particao de ~11,8 MB). Se nao couber, a qualidade
baixa um pouco; se ainda nao couber, o fim do video e' cortado.
"""
import hashlib
import os
import shutil
import subprocess
import tempfile

from fastapi import HTTPException

LARGURA, ALTURA, FPS = 1024, 600, 20
LIMITE_BYTES = 11_000_000          # cabe na particao LittleFS da tela com folga
DURACAO_MAX_S = 120                # nao processa mais que isso do video enviado
QUALIDADES = (5, 7, 9, 12)         # -q:v do MJPEG: menor = melhor e maior
QUALIDADE_CORTE = 12
TIMEOUT_FFMPEG_S = 240


def caminho_ffmpeg() -> str:
    """ffmpeg do pacote imageio-ffmpeg (vem dentro do pip, funciona no Render
    com ou sem Docker); se nao houver, o ffmpeg do sistema."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    sistema = shutil.which("ffmpeg")
    if sistema:
        return sistema
    raise HTTPException(status_code=500, detail="Conversor de video (ffmpeg) indisponivel no servidor")


def contar_quadros(dados: bytes) -> int:
    # Cada quadro MJPEG comeca com FF D8 FF; dentro dos dados comprimidos FF D8 nao aparece.
    return dados.count(b"\xff\xd8\xff")


def _converter(ffmpeg: str, entrada: str, saida: str, qualidade: int, quadros: int | None = None) -> bytes:
    filtro = (
        f"scale={LARGURA}:{ALTURA}:force_original_aspect_ratio=decrease,"
        f"pad={LARGURA}:{ALTURA}:(ow-iw)/2:(oh-ih)/2,fps={FPS}"
    )
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", entrada, "-t", str(DURACAO_MAX_S),
           "-vf", filtro, "-pix_fmt", "yuvj420p", "-q:v", str(qualidade), "-an"]
    if quadros:
        cmd += ["-frames:v", str(quadros)]
    cmd += ["-f", "mjpeg", saida]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT_FFMPEG_S)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=422, detail="O video demorou demais para converter; envie um video mais curto") from exc
    if r.returncode != 0 or not os.path.exists(saida):
        motivo = r.stderr.decode(errors="ignore").strip()[-200:]
        raise HTTPException(status_code=422, detail=f"Nao foi possivel converter o video ({motivo or 'formato nao reconhecido'})")
    with open(saida, "rb") as f:
        return f.read()


def converter_para_tela(caminho_entrada: str) -> tuple[bytes, int]:
    """Devolve (mjpeg, quadros) com no maximo LIMITE_BYTES."""
    ffmpeg = caminho_ffmpeg()
    with tempfile.TemporaryDirectory() as pasta:
        saida = os.path.join(pasta, "video.mjpeg")
        dados = b""
        for qualidade in QUALIDADES:
            dados = _converter(ffmpeg, caminho_entrada, saida, qualidade)
            if len(dados) <= LIMITE_BYTES:
                break
        else:
            # Nem com qualidade menor coube: corta o fim do video.
            quadros = contar_quadros(dados)
            cabem = int(quadros * LIMITE_BYTES / max(len(dados), 1) * 0.95)
            if cabem < FPS:  # menos de 1 s nao vale a pena
                raise HTTPException(status_code=422, detail="Video pesado demais para a tela; envie um video mais simples")
            dados = _converter(ffmpeg, caminho_entrada, saida, QUALIDADE_CORTE, quadros=cabem)
    quadros = contar_quadros(dados)
    if not dados or quadros == 0:
        raise HTTPException(status_code=422, detail="O video convertido ficou vazio; confira o arquivo enviado")
    return dados, quadros


def versao_do_video(dados: bytes) -> str:
    return hashlib.sha256(dados).hexdigest()[:16]

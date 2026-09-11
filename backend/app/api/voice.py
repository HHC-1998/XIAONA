"""语音路由（04 §7/§9）：ASR 转写 + TTS 合成，均需认证。"""
import asyncio
import logging
import os
import re
import tempfile
import unicodedata

from fastapi import APIRouter, UploadFile, File, Depends
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app.core.auth import get_current_user
from app.core.asr_provider import asr
from app.core.audio_utils import convert_to_wav
from app.core.media_utils import MAX_FILE_SIZE
from app.core.tts_provider import synthesize
from app.db.sql_models import User

router = APIRouter(prefix="/api/voice", tags=["voice"])

logger = logging.getLogger("xiaona")


def _strip_markdown(text: str) -> str:
    """TTS 前的 markdown 清理兜底（04 §9 / 08 §2.4）：前端 stripMarkdown 之外的双保险。

    去掉代码块/行内代码/标题/列表符号/链接/加粗斜体/emoji 等，避免语音念出 * # 等符号
    或表情描述文字。
    """
    text = text or ""
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*([-*+]|\d+\.)\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)
    text = re.sub(r"(\*|_)(.*?)\1", r"\2", text)
    text = re.sub(r"~~(.*?)~~", r"\1", text)
    text = re.sub(r"^\s*\|[\s\-:|]+\|\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\|", "，", text)
    text = re.sub(r"<[^>]+>", "", text)
    # 表情符号/图形符号（Unicode So 类别）与 ZWJ/变体选择符：语音只念纯文本
    text = "".join(
        c for c in text
        if unicodedata.category(c) != "So" and c not in ("\u200d", "\ufe0f")
    )
    return re.sub(r"\s+", " ", text).strip()


@router.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    # 转写大小预检：超过 10MB 直接拒绝（与上传限制一致，防超大音频读进内存/ffmpeg 挂起）
    if file.size is not None and file.size > MAX_FILE_SIZE:
        return JSONResponse({"detail": "音频文件过大（超过 10MB），无法转写"}, status_code=422)
    suffix = os.path.splitext(file.filename or "")[-1] or ".webm"
    wav_path = None
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # 统一转 wav：已是前端解码的 16kHz 单声道 WAV 时直接透传（不再走 ffmpeg），
        # 其余格式（飞书/历史兼容）由 ffmpeg 兜底转换
        wav_path = await asyncio.to_thread(convert_to_wav, tmp_path)
        text = await asr.transcribe(wav_path)
        return JSONResponse({"text": text})
    except Exception as e:
        # 转写失败返回友好中文错误，前端据此展示具体原因，不再裸 500
        logger.warning("voice transcribe failed: %s", e, exc_info=True)
        return JSONResponse({"detail": "音频转写失败，请重试或稍后再试"}, status_code=422)
    finally:
        os.unlink(tmp_path)
        # 透传时 wav_path 即 tmp_path（已删除）；其余为新建 *_16k.wav 需单独清理
        if wav_path and wav_path != tmp_path and os.path.exists(wav_path):
            os.unlink(wav_path)


class TTSRequest(BaseModel):
    text: str


@router.post("/tts")
async def tts(
    req: TTSRequest,
    user: User = Depends(get_current_user),
):
    # 去 markdown + emoji 后再合成，避免语音念出 * # 等符号或表情描述（前端 stripMarkdown 之外的后端兜底）
    cleaned = _strip_markdown(req.text)
    # 长度上限：清洗后超限直接拒绝，防超长文本长时间占用合成/连接（桌面助手按句合成，正常远小于此值）
    if len(cleaned) > 5000:
        return JSONResponse({"detail": "文本过长（超过 5000 字符），无法合成语音"}, status_code=422)
    try:
        audio_path = await synthesize(cleaned or "嗯")
    except Exception as e:
        # 合成失败（edge-tts 断网/超时等）返回友好中文错误，前端据此提示，不再裸 500
        logger.warning("tts synthesize failed: %s", e, exc_info=True)
        return JSONResponse({"detail": "语音合成失败，请稍后重试"}, status_code=422)
    # background 挂在响应上：文件流发送完成后删除临时 mp3，避免堆积
    return FileResponse(
        audio_path,
        media_type="audio/mpeg",
        background=BackgroundTask(os.unlink, audio_path),
    )

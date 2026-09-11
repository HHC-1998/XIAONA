"""飞书客户端（10 §6）：httpx.AsyncClient + tenant token 内存缓存 + 统一重试。"""
import os
import time
import json
import tempfile
import logging
import httpx

logger = logging.getLogger(__name__)


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.client = httpx.AsyncClient(timeout=30)
        self._token: str | None = None
        self._token_expire_at: float = 0.0

    async def _request(self, method: str, url: str, *, headers=None, json=None, params=None):
        """统一请求 + 重试：非 2xx 响应与 httpx.RequestError（超时/断连）都重试 2 次并记日志。
        最后一次仍失败时抛错（webhook 层可据此返回 500 触发飞书重试）。"""
        for attempt in (1, 2):
            try:
                resp = await self.client.request(method, url, headers=headers, json=json, params=params)
                if resp.status_code < 300:
                    return resp
                logger.warning(f"feishu {method} {url} failed (attempt {attempt}): {resp.status_code} {resp.text[:200]}")
            except httpx.RequestError as e:
                logger.warning(f"feishu {method} {url} network error (attempt {attempt}): {e}")
        raise RuntimeError(f"feishu {method} {url} failed after retries")

    async def get_tenant_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expire_at - 60:  # 提前 60s 刷新
            return self._token
        resp = await self._request(
            "POST",
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        data = resp.json()
        self._token = data["tenant_access_token"]
        self._token_expire_at = now + data.get("expire", 7200)
        return self._token

    async def send_message(self, chat_id: str, text: str):
        if not chat_id:
            raise ValueError("chat_id 不能为空")
        MAX_TEXT_LEN = 1500
        if len(text) > MAX_TEXT_LEN:
            text = text[: MAX_TEXT_LEN - 20] + "\n……（内容过长已截断）"
        token = await self.get_tenant_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        body = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        await self._request(
            "POST", url, headers=headers, params={"receive_id_type": "chat_id"}, json=body
        )

    async def download_resource(self, message_id: str, file_key: str,
                                resource_type: str = "file") -> bytes:
        """下载消息资源（10 §6）：GET /messages/{id}/resources/{file_key}。

        飞书资源接口的 type 参数区分资源类别：image（图片）必须传 type=image，
        file（普通文件）/ 音频等传 type=file。此前统一传 type=file 导致单聊/群聊@图片
        下载必失败（2026-08-24 审核修复，见当天台账）。

        下载后校验大小上限（MAX_FILE_SIZE 10MB），超限直接抛错，防恶意超大资源整包进内存
        （webhook 层 catch 记录，2026-08-17 修）。
        """
        from app.core.media_utils import MAX_FILE_SIZE
        token = await self.get_tenant_token()
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
        resp = await self._request(
            "GET", url, headers={"Authorization": f"Bearer {token}"},
            params={"type": resource_type},
        )
        if len(resp.content) > MAX_FILE_SIZE:
            raise ValueError(f"resource {file_key} too large: {len(resp.content)} bytes > {MAX_FILE_SIZE}")
        return resp.content

    async def download_audio(self, message_id: str, file_key: str) -> bytes:
        return await self.download_resource(message_id, file_key, resource_type="file")

    async def transcribe_audio(self, audio_bytes: bytes) -> str:
        """内部调用共享 ASR 封装完成转写（与 04 一致）。"""
        import asyncio
        from app.core.asr_provider import asr
        from app.core.audio_utils import convert_to_wav
        with tempfile.NamedTemporaryFile(suffix=".opus", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        wav_path = None
        try:
            wav_path = await asyncio.to_thread(convert_to_wav, tmp_path)
            return await asr.transcribe(wav_path)
        finally:
            os.unlink(tmp_path)
            if wav_path and os.path.exists(wav_path):
                os.unlink(wav_path)

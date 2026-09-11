"""TTS 语音合成（04 §9）：edge-tts 在线合成，返回 mp3 临时文件路径。

依赖联网；output_path 为空时自动创建按请求唯一命名的临时文件。
"""
import asyncio
import tempfile
import uuid
import os

# edge-tts 整段合成超时：网络异常时避免请求无限挂起（2026-08-16 晚审查补）
TTS_TIMEOUT = 30


async def synthesize(text: str, output_path: str = None):
    import edge_tts   # 惰性 import：未安装 edge-tts 时核心仍可启动
    temp_created = False
    if not output_path:
        fd, output_path = tempfile.mkstemp(suffix=".mp3", prefix=f"tts_{uuid.uuid4().hex}_")
        os.close(fd)
        temp_created = True
    try:
        communicate = edge_tts.Communicate(text, voice="zh-CN-XiaoxiaoNeural")
        await asyncio.wait_for(communicate.save(output_path), timeout=TTS_TIMEOUT)
    except Exception:
        if temp_created:
            os.unlink(output_path)
        raise
    return output_path

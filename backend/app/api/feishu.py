"""飞书回调（10 §4/§5.1 / 12 §5）：challenge 验证 + 签名验证 + 消息事件路由（含媒体按需监听）。

处理顺序（2026-08-18 起）：
- 先解密（事件加密模式）→ challenge 直接返回 → 正式事件才验签；
- 绑定校验分流：未绑定 open_id 不创建账号、不调 LLM，只走绑定状态机 / 0 token 引导；
- 已绑定用户 → 会话映射 → 媒体/音频/文本分流 → agent/chat → 回复发送。
- 事件去重采用「后置标记」：处理成功才记 seen，失败不记（飞书重试同 event 仍会处理）。
- 媒体按需监听策略（12 §5，用户确认）：
  - 群聊未 @ 的 image/file/media 消息 → 只轻量记录「谁发了什么媒体」元数据（不下载/不调 LLM/不回复）；
  - 群聊 @ 或单聊的 image/file → 按需下载解析，交给多模态对话；模型不支持时以聊天式回复反馈；
  - 视频（media）→ 只记元数据、不拉取（转写成本高）；表情包（sticker）完全忽略；
  - 音频（audio）→ 维持实时转写。
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

from app import config
from app.core.agent import agent_service
from app.core.auth import verify_password, send_sms_code, verify_sms_code
from app.core.memory_engine import chat_core, sql as memory_sql
from app.core.feishu_client import FeishuClient
from app.core.media_utils import KIND_LABELS, detect_kind, parse_upload, unsupported_reply
from app.db.feishu_session_map import get_feishu_bind_user, bind_feishu_user, ensure_feishu_session
from app.db.database import SessionLocal
from app.db.sql_models import User

router = APIRouter(prefix="/api/feishu", tags=["feishu"])

logger = logging.getLogger("xiaona")

# 签名验证 key：飞书事件加密模式下用 Encrypt Key，纯签名验证模式下用 Verification Token。
# 飞书官方算法：sha256(timestamp + nonce + key + body)，key 与加密策略一致（2026-08-17）。
FEISHU_VERIFY_TOKEN = config.FEISHU_VERIFY_TOKEN
FEISHU_ENCRYPT_KEY = config.FEISHU_ENCRYPT_KEY
BOT_OPEN_ID = config.FEISHU_BOT_OPEN_ID
BOT_NAME = config.FEISHU_BOT_NAME
feishu_client = FeishuClient(config.FEISHU_APP_ID, config.FEISHU_APP_SECRET)


# ---------- 账号绑定状态机（10 §5.1，2026-08-18 起） ----------
# 未绑定 open_id 的绑定交互状态按 open_id 存内存（含密码/验证码敏感中间态，
# 不落库、不进记忆、不进日志），5 分钟超时。重启即清零。
_BIND_STATE: dict[str, dict] = {}   # open_id -> {step, phone, expire}
_BIND_TTL = 300                      # 绑定状态 5 分钟超时
# 手机号正则放宽（2026-08-24 修）：原 ^1\d{10}$ 只认中国移动网段开头，
# 支持非 1 开头号码（如虚拟运营商 9 号段/后续号段），取 6~15 位数字的通用区间
_PHONE_RE = re.compile(r"^\d{6,15}$")
_BIND_KEYWORDS = ("绑定", "绑定账号", "连接账号", "连接小娜", "绑定飞书")
_RESEND_KEYWORDS = ("重发", "重新发送", "再发一次", "重发验证码")

_UNBOUND_GUIDE = (
    "你好，我是小娜。你还没有连接到小娜账号，暂时无法与我对话。\n"
    "1. 还没有账号：请先到 XIAONA 网页注册。\n"
    "2. 已有账号：回复「绑定」，或直接回复你的注册手机号，按提示完成绑定即可正常使用。"
)


def _mask_phone(phone: str) -> str:
    """脱敏手机号：159****4248。"""
    return (phone[:3] + "****" + phone[-4:]) if len(phone) >= 7 else phone


def _bind_step_phone(phone: str, open_id: str) -> str:
    """绑定第 1 步：报手机号 → 查账号（不存在则引导注册）。"""
    with SessionLocal() as db:
        u = db.query(User).filter_by(phone=phone).first()
    if u is None:
        return "该手机号未注册。请先到 XIAONA 网页注册账号后再来绑定。"
    _BIND_STATE[open_id] = {"step": "await_password", "phone": phone,
                            "expire": time.time() + _BIND_TTL}
    return f"手机号 {_mask_phone(phone)} 存在。请发送你的小娜账号密码。"


def _bind_step_password(text: str, open_id: str, state: dict) -> str:
    """绑定第 2 步：报密码 → 校验（错误提示改密，不退回手机号）。"""
    phone = state["phone"]
    with SessionLocal() as db:
        u = db.query(User).filter_by(phone=phone).first()
    if u is None:
        _BIND_STATE.pop(open_id, None)
        return "该手机号未注册。请先注册后再来绑定。"
    if not verify_password(u, text):
        return "密码错误，请到 XIAONA 网页「设置 → 账号」中修改密码后重试。"
    try:
        send_sms_code(phone)
    except HTTPException as e:
        return e.detail
    _BIND_STATE[open_id] = {"step": "await_code", "phone": phone,
                            "expire": time.time() + _BIND_TTL}
    return f"密码验证通过。验证码已发送到手机 {_mask_phone(phone)}，请查收后回复验证码（5 分钟内有效）。"


def _bind_step_code(text: str, open_id: str, state: dict) -> str:
    """绑定第 3 步：报验证码 → 校验 → 写入 feishu_user_map → 绑定成功。"""
    phone = state["phone"]
    try:
        verify_sms_code(phone, text)
    except HTTPException as e:
        # 验证码错误/过期：附带「重发」入口（2026-08-24 起），
        # 用户回复「重发」即可重新发送验证码，不必从头走绑定流程
        if "过期" in str(e.detail) or "重新获取" in str(e.detail) or "次数过多" in str(e.detail):
            return f"{e.detail}。回复「重发」可重新发送。"
        return e.detail
    with SessionLocal() as db:
        u = db.query(User).filter_by(phone=phone).first()
        if u is None:
            _BIND_STATE.pop(open_id, None)
            return "账号异常，请稍后重试绑定。"
        user_id = u.id
    bind_feishu_user(open_id, user_id)
    _BIND_STATE.pop(open_id, None)
    logger.info("feishu open_id %s bound to user %s", open_id, user_id)
    return f"绑定成功！你已关联手机号 {_mask_phone(phone)}，从现在起我们的对话会保存到该小娜账号。"


def _handle_bind_turn(text: str, open_id: str) -> str | None:
    """未绑定用户消息入口：返回要发送的回复文本（None 则不回复）。"""
    now = time.time()
    state = _BIND_STATE.get(open_id)
    if state and now > state["expire"]:
        _BIND_STATE.pop(open_id, None)
        state = None

    if not state:
        if _PHONE_RE.match(text.strip()):
            return _bind_step_phone(text.strip(), open_id)
        if text.strip() in _BIND_KEYWORDS:
            return "好的，开始绑定你的小娜账号。\n请发送你的注册手机号（11 位，如 15912345678）。"
        return _UNBOUND_GUIDE

    if state["step"] == "await_password":
        return _bind_step_password(text, open_id, state)
    if state["step"] == "await_code":
        # 验证码过期/输错后的「重发」入口（2026-08-24 起）：
        # 不重置绑定状态机，仅重新发送验证码
        if text.strip() in _RESEND_KEYWORDS:
            phone = state["phone"]
            with SessionLocal() as db:
                u = db.query(User).filter_by(phone=phone).first()
            if u is None:
                _BIND_STATE.pop(open_id, None)
                return "该手机号未注册，绑定已终止。请重新绑定。"
            try:
                send_sms_code(phone)
            except HTTPException as e:
                return e.detail
            state["expire"] = time.time() + _BIND_TTL
            return f"验证码已重新发送到手机 {_mask_phone(phone)}，请查收后回复（5 分钟内有效）。"
        return _bind_step_code(text, open_id, state)
    return _UNBOUND_GUIDE


# ---------- 加解密与验签 ----------
def _decrypt_feishu_body(body: bytes, encrypt_key: str) -> str:
    """解密飞书事件加密请求体（AES-256-CBC，官方算法，2026-08-17 修）。

    飞书官方解密逻辑：
    - key = sha256(Encrypt Key).digest()（32 字节）
    - 收到的 `encrypt` 字符串 base64 解码后，前 16 字节为随机 IV，剩余为密文
    - AES-256-CBC + PKCS7 填充解密，解出明文 JSON 字符串
    """
    enc = base64.b64decode(body)
    iv = enc[: AES.block_size]
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = unpad(cipher.decrypt(enc[AES.block_size:]), AES.block_size)
    return decrypted.decode("utf-8")


# 已处理事件去重：单 worker 下有效；多 worker 建议用 Redis/DB 存最近 event_id
_seen_events: dict[str, float] = {}   # event_id -> 处理时间戳（秒）
_EVENT_TTL = 3600                     # 1 小时 TTL

# 飞书群聊 @ 占位符（纯文本 @_user_1 / 富文本 <at>）送入 LLM 前都需去除
_MENTION_RE = re.compile(r"@_user_[\w-]+|<at[^>]*user_id=\"[^\"]*\"[^>]*>|</at>")

# 群聊 @ 文字中触发"按需拉取最近媒体"的关键词（12 §5）
_MEDIA_KEYWORDS = ("图", "图片", "照片", "截图", "文件", "文档", "PDF", "Word", "Excel", "表格")


def is_duplicate_event(event_id: str) -> bool:
    """只读判断是否已处理（不写入）。"""
    now = time.time()
    for eid in list(_seen_events):
        if now - _seen_events[eid] > _EVENT_TTL:
            _seen_events.pop(eid, None)
    return event_id in _seen_events


def mark_event_done(event_id: str) -> None:
    """处理成功后标记为已完成（供重试判定）。"""
    _seen_events[event_id] = time.time()


def extract_text(event) -> str:
    """从消息事件中提取文字内容；audio 消息无文本（返回空，由 ASR 转写）。"""
    message = event["message"]
    msg_type = message.get("message_type")
    if msg_type == "text":
        raw = json.loads(message["content"]).get("text", "")
        return _MENTION_RE.sub("", raw).strip()
    return ""


def is_bot_mentioned(event) -> bool:
    message = event["message"]
    if message.get("chat_type") != "group":
        return True
    mentions = message.get("mentions", [])
    for m in mentions:
        # 以机器人 open_id 精确匹配为主（不受名字大小写/全角空格影响）
        if m.get("id", {}).get("open_id") == BOT_OPEN_ID:
            return True
        # name 可能含全角空格/大小写差异，统一小写后比较，作为兜底
        if (m.get("name") or "").strip().lower() == BOT_NAME.lower():
            return True
    return False


# ---------- 媒体按需监听（12 §5） ----------
def _parse_media_meta(message) -> dict | None:
    """从飞书消息事件解析媒体元数据：{name, kind, size, file_key, message_id}。

    - image：image_key → kind=image；
    - file：file_key + file_name → 按扩展名判定 kind（未知扩展名记为 unsupported）；
    - media（视频）：file_key → kind=video（降级：只记元数据、不拉取）。
    """
    msg_type = message.get("message_type")
    try:
        content = json.loads(message.get("content") or "{}")
    except Exception:
        return None
    message_id = message.get("message_id", "")
    if msg_type == "image":
        file_key = content.get("image_key") or ""
        if not file_key:
            return None
        ext = content.get("image_type") or "png"
        if ext not in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
            ext = "png"
        return {"name": f"图片.{ext}", "kind": "image", "size": 0,
                "file_key": file_key, "message_id": message_id}
    if msg_type == "file":
        file_key = content.get("file_key") or ""
        if not file_key:
            return None
        name = content.get("file_name") or ""
        kind = detect_kind(name, "")
        if kind == "unsupported":
            kind = "file"
        return {"name": name, "kind": kind, "size": content.get("file_size") or 0,
                "file_key": file_key, "message_id": message_id}
    if msg_type == "media":
        file_key = content.get("file_key") or ""
        if not file_key:
            return None
        return {"name": content.get("file_name") or "视频", "kind": "video",
                "size": content.get("file_size") or 0,
                "file_key": file_key, "message_id": message_id}
    return None


async def _record_media_meta(user_id: str, session_id: str, event) -> None:
    """群聊未 @ 的媒体消息：轻量落库一条元数据 user 消息（12 §5），不调 LLM、不回复。"""
    meta = _parse_media_meta(event.get("message", {}))
    if not meta:
        return
    kind_label = KIND_LABELS.get(meta["kind"], "文件")
    name = meta.get("name") or ""
    text = f"[用户发送了{kind_label}" + (f"：{name}" if name else "") + "]"
    from app.core import memory_engine
    with SessionLocal() as db:
        cfgs = memory_engine._model_configs(db, user_id)  # 与正常聊天一致的向量化门控
    # to_thread 下沉：同步落库 + Chroma embedding（HTTP 向量化调用，
    # 在 async webhook 里直接调用会阻塞事件循环，影响同进程的 chat_stream）
    await asyncio.to_thread(
        memory_sql.save_chat_message, user_id, session_id, "user", text, "feishu",
        cfgs["embedding"], "", json.dumps([meta], ensure_ascii=False),
    )


def _latest_media(user_id: str, session_id: str) -> dict | None:
    """取会话内最近一条带可下载媒体（file_key）的附件元数据（12 §5 按需拉取）。

    遍历最近若干条（limit 10）：最新带附件消息可能恰好经 _attachments_meta 路径落库时
    file_key 为空，需继续往前扫（2026-08-17 修）。
    """
    from app.db.sql_models import Message
    with SessionLocal() as db:
        msgs = (
            db.query(Message)
            .filter(Message.session_id == session_id, Message.user_id == user_id,
                    Message.attachments.isnot(None), Message.attachments != "")
            .order_by(Message.created_at.desc())
            .limit(10).all()
        )
    for msg in msgs:
        try:
            atts = json.loads(msg.attachments)
        except Exception:
            continue
        hit = next((a for a in atts if a.get("file_key") and a.get("kind") != "video"), None)
        if hit:
            return hit
    return None


def _wants_media(text: str) -> bool:
    """群聊 @ 文字是否暗示要看图/分析文件（触发按需拉取最近媒体）。"""
    return any(k in (text or "") for k in _MEDIA_KEYWORDS)


def _mime_for_media(meta: dict) -> str:
    """按元数据推断下载后的 MIME（图片按扩展名校正，供 parse_upload 生成 data_url）。"""
    kind = meta.get("kind")
    name = meta.get("name") or ""
    if kind == "image":
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else "png"
        return f"image/{'jpeg' if ext == 'jpg' else ext}"
    return {
        "pdf": "application/pdf",
        "word": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "excel": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text": "text/plain",
    }.get(kind, "application/octet-stream")


async def _handle_media_message(user: User, session_id: str, chat_id: str, event) -> None:
    """被 @ 或单聊的 image/file：按需下载解析 → 多模态对话 → 回复（12 §5）。"""
    message = event["message"]
    meta = _parse_media_meta(message)
    if not meta:
        return
    text = extract_text(event) or ""
    # video 不在此处理：webhook 已对 msg_type=="media" 单独分流 _record_media_meta
    #（视频只记元数据、不拉取、不回复，12 §5），本函数只进 image/file
    if meta["kind"] == "unsupported":
        reply = unsupported_reply(meta["name"] or "该文件")
        await chat_core.chat_media_reply(user.id, session_id, text, reply, "feishu", [meta])
        await feishu_client.send_message(chat_id, reply)
        return
    # 下载/解析失败兜底（2026-08-24 修）：此前下载/解析抛异常会使 webhook 处理
    # 直接中断、用户无任何回复；现在降级为友好聊天式回复落库回发
    try:
        data = await feishu_client.download_resource(
            meta["message_id"], meta["file_key"],
            resource_type="image" if meta.get("kind") == "image" else "file",
        )
        # parse_upload CPU 密集（pdf/docx/xlsx 解析）下沉线程池，防 webhook 阻塞事件循环
        att = await asyncio.to_thread(
            parse_upload, meta.get("name", ""), _mime_for_media(meta), data
        )
    except Exception as e:
        logger.warning("feishu media download/parse failed: %s", e, exc_info=True)
        reply = "抱歉，我暂时无法获取这个文件/图片（下载或解析失败），请稍后再试。"
        await chat_core.chat_media_reply(user.id, session_id, text, reply, "feishu", [meta])
        await feishu_client.send_message(chat_id, reply)
        return
    if att["kind"] == "unsupported":
        reply = unsupported_reply(meta.get("name") or "该文件",
                                  too_large=att.get("reason") == "too_large")
        await chat_core.chat_media_reply(user.id, session_id, text, reply, "feishu", [meta])
        await feishu_client.send_message(chat_id, reply)
        return
    await _media_aware_reply(user, session_id, chat_id, text, [att])
    return
    # 注意：后续下载/解析异常统一由 _media_aware_reply 内 try/except 兜底，
    # 以友好聊天式回复落库并回发，不裸 500 也不让用户看到内部错误码（12 §4 约定）


async def _media_aware_reply(user: User, session_id: str, chat_id: str, text: str,
                             attachments: list[dict]) -> None:
    """带附件走 agent → 回落 chat 的回复封装（2026-08-24 起）。

    把 agent_service.handle / chat_core.chat 的异常兜底为友好聊天式回复落库回发——
    此前下载/解析之后的多模态对话若抛异常（模型不支持/网络抖动），webhook 处理中断
    且没有任何回复，用户会以为机器人没收到。媒体类错误绝不上屏错误码（12 §4）。
    """
    async def _notify(msg: str, _chat_id=chat_id) -> None:
        try:
            await feishu_client.send_message(_chat_id, msg)
        except Exception:
            logger.warning("feishu media notify send failed", exc_info=True)
    try:
        result = await agent_service.handle(
            text, user_id=user.id, session_id=session_id, source="feishu",
            attachments=attachments, notify=_notify,
        )
        if result.reply is not None:
            reply = result.reply
        else:
            reply, _ = await chat_core.chat(text, user_id=user.id, session_id=session_id,
                                            source="feishu", attachments=attachments)
    except Exception as e:
        logger.warning("feishu media reply failed, degrade to friendly reply: %s", e, exc_info=True)
        reply = "抱歉，我暂时无法处理这个文件/图片，请稍后再试或换一个文件试试。"
        try:
            await chat_core.chat_media_reply(user.id, session_id, text, reply, "feishu",
                                             attachments)
        except Exception:
            logger.warning("feishu media fallback persist failed", exc_info=True)
    await feishu_client.send_message(chat_id, reply)


# ---------- Webhook 入口 ----------
@router.post("/webhook")
async def webhook(request: Request):
    body = await request.body()

    # 0. 解析外层 body：事件加密模式下为 {"encrypt":"..."} 密文，先解密得到明文 JSON。
    #    注意：challenge 验证请求（url_verification）虽然加密，但不携带签名头，
    #    因此必须「先解密 → 判 challenge → 正式事件才验签」（2026-08-17 修）。
    raw = json.loads(body)
    if "encrypt" in raw:
        if not FEISHU_ENCRYPT_KEY:
            raise HTTPException(status_code=400, detail="Encrypted event but FEISHU_ENCRYPT_KEY not configured")
        payload = json.loads(_decrypt_feishu_body(raw["encrypt"], FEISHU_ENCRYPT_KEY))
    else:
        payload = raw

    # 1. Challenge 验证（首次配置回调时）：不带签名头，直接解密后原样返回 challenge
    if payload.get("type") == "url_verification" or "challenge" in payload:
        return JSONResponse({"challenge": payload["challenge"]})

    # 2. 正式事件：签名验证（challenge 之外的推送都带签名头）
    # 飞书官方算法：sha256(timestamp + nonce + encrypt_key + body).hexdigest()，直接拼接无分隔符。
    # 注意：不是 HMAC；key 与加密策略一致——配置了 Encrypt Key 用 Encrypt Key，否则用 Verification Token。
    sign_key = FEISHU_ENCRYPT_KEY or FEISHU_VERIFY_TOKEN
    b1 = (
        request.headers.get("X-Lark-Request-Timestamp", "")
        + request.headers.get("X-Lark-Request-Nonce", "")
        + sign_key
    ).encode("utf-8")
    expected = hashlib.sha256(b1 + body).hexdigest()
    if not hmac.compare_digest(request.headers.get("X-Lark-Signature", ""), expected):
        logger.warning(
            "[feishu-webhook] signature mismatch: ts=%s nonce=%s sign_key_len=%d body_len=%d body_head=%r expected=%s... got=%s... headers=%s",
            request.headers.get("X-Lark-Request-Timestamp", ""),
            request.headers.get("X-Lark-Request-Nonce", ""),
            len(sign_key),
            len(body),
            body[:40],
            expected[:12],
            request.headers.get("X-Lark-Signature", "")[:12],
            {k: v for k, v in request.headers.items() if k.lower().startswith("x-lark")},
        )
        raise HTTPException(status_code=403, detail="Invalid signature")

    # 3. 统一消息处理（webhook 唯一入口）
    event = payload.get("event", {})
    event_id = payload.get("event_id") or event.get("event_id")
    try:
        await process_feishu_event(event, event_id or "")
    except Exception:
        # 不标记 seen → 飞书重试同一 event_id 仍会重新处理，不会静默丢回复
        raise
    return JSONResponse({"code": 0})


async def process_feishu_event(event: dict, event_id: str) -> None:
    """统一消息处理（10 §5）：webhook 唯一入口。

    去重 → 绑定校验（未绑定走 0 token 引导 / 绑定状态机，不落库不调 LLM）→
    会话映射（仅已绑定用户建会话）→ 媒体/音频/文本分流 → agent/chat → 回复发送。
    event_id 为空时跳过去重与完成标记。
    """
    if event_id and is_duplicate_event(event_id):
        return

    message = event.get("message", {})
    msg_type = message.get("message_type")
    # 表情包等完全忽略（12 §5 降级策略）
    if msg_type not in ("text", "audio", "image", "file", "media"):
        if event_id:
            mark_event_done(event_id)
        return

    feishu_user_id = event["sender"]["sender_id"]["open_id"]
    chat_id = message["chat_id"]
    channel_type = message.get("chat_type", "p2p")  # p2p / group

    # 绑定校验：未绑定 open_id 不创建账号、不调 LLM，只走绑定状态机 / 0 token 引导
    user = get_feishu_bind_user(feishu_user_id)
    if user is None:
        # 群聊未 @ 机器人：直接忽略不回复
        if channel_type == "group" and not is_bot_mentioned(event):
            if event_id:
                mark_event_done(event_id)
            return
        # 绑定只在私聊进行（10 §5.1「全程在飞书对话框内分步引导」）：
        # 群聊 @ 时若进入绑定状态机会把手机号/密码/短信验证码明文发到群聊记录里
        # 被全员看到，属隐私事故（2026-08-24 审核修复）
        if channel_type != "p2p":
            reply = "为了账号安全，绑定小娜请在私聊中进行。请私聊小娜后回复「绑定」。"
            await feishu_client.send_message(chat_id, reply)
            if event_id:
                mark_event_done(event_id)
            return
        text = extract_text(event)
        reply = _handle_bind_turn(text, feishu_user_id) if text else _UNBOUND_GUIDE
        if reply:
            await feishu_client.send_message(chat_id, reply)
        if event_id:
            mark_event_done(event_id)
        return

    # 已绑定：确保会话映射（仅绑定用户建会话；顺带刷新 last_active_at 活跃统计）
    session_id = ensure_feishu_session(user, feishu_user_id, chat_id, channel_type)

    # 群聊未 @ 的媒体消息：只记元数据、不调 LLM、不回复（12 §5 按需监听策略）
    if channel_type == "group" and not is_bot_mentioned(event):
        if msg_type in ("image", "file", "media"):
            await _record_media_meta(user.id, session_id, event)
        if event_id:
            mark_event_done(event_id)
        return

    if msg_type in ("image", "file"):
        await _handle_media_message(user, session_id, chat_id, event)
    elif msg_type == "media":
        # 视频降级：只记元数据、不拉取、不回复（12 §5）
        await _record_media_meta(user.id, session_id, event)
    elif msg_type == "audio":
        audio_key = json.loads(message["content"]).get("file_key", "")
        audio_bytes = await feishu_client.download_audio(message["message_id"], audio_key)
        text = await feishu_client.transcribe_audio(audio_bytes)
        async def _notify(msg: str, _chat_id=chat_id) -> None:
            await feishu_client.send_message(_chat_id, msg)
        result = await agent_service.handle(
            text, user_id=user.id, session_id=session_id, source="feishu",
            notify=_notify,
        )
        if result.reply is not None:
            reply = result.reply
        else:
            reply, _ = await chat_core.chat(
                text, user_id=user.id, session_id=session_id, source="feishu"
            )
        await feishu_client.send_message(chat_id, reply)
    else:  # text
        text = extract_text(event)
        attachments = None
        # 群聊 @ 文字中要求"看图/分析文件"时，自动按需拉取会话内最近媒体（12 §5）
        if channel_type == "group" and text and _wants_media(text):
            media = _latest_media(user.id, session_id)
            if media:
                try:
                    data = await feishu_client.download_resource(
                        media["message_id"], media["file_key"],
                        resource_type="image" if media.get("kind") == "image" else "file",
                    )
                    att = await asyncio.to_thread(
                        parse_upload, media.get("name", ""), _mime_for_media(media), data
                    )
                    if att["kind"] != "unsupported":
                        attachments = [att]
                except Exception as e:
                    logger.warning("feishu on-demand media download failed: %s", e)
        # 任务完成回发：notify 回调在任务结束后把结果主动发回飞书
        async def _notify(msg: str, _chat_id=chat_id) -> None:
            await feishu_client.send_message(_chat_id, msg)
        result = await agent_service.handle(
            text, user_id=user.id, session_id=session_id, source="feishu",
            attachments=attachments, notify=_notify,
        )
        if result.reply is not None:
            reply = result.reply
        else:
            reply, _ = await chat_core.chat(
                text, user_id=user.id, session_id=session_id, source="feishu",
                attachments=attachments,
            )
        await feishu_client.send_message(chat_id, reply)

    # 全部处理成功后才标记已完成
    if event_id:
        mark_event_done(event_id)

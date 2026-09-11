"""网页抓取工具（02 §7）：httpx 抓取网页正文文本供执行者分析。

零新依赖（httpx 已用于 LLM 调用）；只提取正文文本，图片/JS/CSS 一律丢弃，
避免把无关噪声喂给执行者模型。单页默认截断 8k 字符防上下文爆炸。

SSRF 防护（2026-08-24 起）：抓取前解析 URL host 并做 DNS 解析，拒绝回环/内网/
链路本地/保留/云元数据（169.254.169.254）地址——防止恶意 url 参数让后端充当代理
访问内网服务（如 http://169.254.169.254/latest/meta-data 窃取云凭证）。重定向手动
跟随（follow_redirects=False，最多 5 跳），每跳重新做 SSRF 校验——防公网 URL 302
到内网地址绕过防护。
"""
import ipaddress
import logging
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger("xiaona")


class SSRFBlockedError(Exception):
    """SSRF 校验拦截（含重定向跳转中被拦截的地址）。"""

# 抓取超时与默认截断长度（8k 字符足够分析，过长由执行者按需二次裁剪）
_TIMEOUT = 30
_MAX_TEXT = 8000
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 禁止访问的地址段（含云元数据地址 169.254.169.254）
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),     # 链路本地 + 云元数据
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),      # CGNAT
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),           # IPv6 ULA
    ipaddress.ip_network("fe80::/10"),          # IPv6 链路本地
]


def _is_blocked(ip: str) -> bool:
    """判断 IP 是否落在禁止访问的地址段内（含回环/内网/保留）。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # 无法解析的地址一律拒绝
    if not addr.is_global:
        return True  # 非公网单播（回环/私网/链路本地/组播/保留）一律拒绝
    return any(addr in net for net in _BLOCKED_NETWORKS)


def _ssrf_check(url: str) -> str | None:
    """SSRF 预检。返回 None 表示放行，否则返回错误信息。"""
    try:
        host = urlparse(url).hostname
    except ValueError:
        return f"URL 无法解析：{url}"
    if not host:
        return f"URL 缺少主机名：{url}"
    # 字面 IP 直接判段
    if re.match(r"^[\d.]+$", host):
        if _is_blocked(host):
            return f"目标地址不允许访问（{host}）"
        return None
    # 域名：DNS 解析后任一地址被禁即拒绝（防 DNS rebinding 打内网）
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return f"域名无法解析：{host}"
    for info in infos:
        ip = info[4][0]
        if _is_blocked(ip):
            return f"目标域名解析到不允许访问的地址（{ip}）"
    return None


def _strip_html(raw: str) -> str:
    """粗略 HTML → 纯文本：去 script/style 与标签、折叠空白。"""
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"&amp;?", "&", text)
    text = re.sub(r"&lt;?", "<", text)
    text = re.sub(r"&gt;?", ">", text)
    text = re.sub(r"&quot;?", '"', text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


async def fetch_url(params: dict, ctx: dict | None = None) -> dict:
    """抓取单个网页正文。params: {url, max_chars?} → {ok, url, title?, text?} 或 {ok, error}。"""
    url = (params or {}).get("url", "").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": f"URL 不合法：{url or '（空）'}，须以 http(s):// 开头"}
    max_chars = int((params or {}).get("max_chars") or _MAX_TEXT)

    async def _fetch_once(client: httpx.AsyncClient, target: str) -> httpx.Response:
        # 每一跳（含初始 URL 与每个重定向目标）都做 SSRF 预检，防公网 302 到内网绕过
        blocked = _ssrf_check(target)
        if blocked:
            raise SSRFBlockedError(f"该地址不允许抓取（{blocked}）")
        return await client.get(target, headers={"User-Agent": _UA})

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            resp = await _fetch_once(client, url)
            current_url = url
            hops = 0
            # 手动跟随 3xx（最多 5 跳），每跳重新做 SSRF 校验
            while resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location") or ""
                if not location:
                    return {"ok": False, "error": f"抓取失败：HTTP {resp.status_code} 且无重定向地址"}
                hops += 1
                if hops > 5:
                    return {"ok": False, "error": f"重定向次数过多（超过 5 跳）：{url}"}
                current_url = urljoin(current_url, location)
                resp = await _fetch_once(client, current_url)
        if resp.status_code != 200:
            return {"ok": False, "error": f"抓取失败：HTTP {resp.status_code}"}
        raw = resp.text
        title = ""
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
        if m:
            title = _strip_html(m.group(1))[:120]
        text = _strip_html(raw)
        if not text:
            return {"ok": True, "url": current_url, "title": title, "text": "（页面无可见文本，可能是 JS 渲染或空白页）"}
        text = text[:max_chars]
        logger.info("fetch_url ok: %s (%d chars)", current_url, len(text))
        return {"ok": True, "url": current_url, "title": title or current_url, "text": text}
    except SSRFBlockedError as e:
        logger.warning("fetch_url SSRF blocked: %s", e.args[0])
        return {"ok": False, "error": e.args[0]}
    except httpx.TimeoutException:
        return {"ok": False, "error": f"抓取超时（{_TIMEOUT}s）：{url}"}
    except httpx.ConnectError as e:
        return {"ok": False, "error": f"无法连接目标网站：{url}（{e.__class__.__name__}）"}
    except Exception as e:
        logger.warning("fetch_url failed: %s", url, exc_info=True)
        return {"ok": False, "error": f"抓取失败：{e.__class__.__name__}"}

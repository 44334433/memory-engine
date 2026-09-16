"""VLM 视觉描述通道（P0 附件层 2026-09-17）：openai_compat 形态，纯 HTTP 客户端。

形态对齐 embedder.OpenAICompatProvider（urllib + Bearer + OpenAI 兼容 /chat/completions）。
配置读环境变量（本批禁改 config.py，故在此读取；后续 config 收编时迁入）：
  - VLM_BASE_URL               OpenAI 兼容端点根（如 http://127.0.0.1:8080/v1）
  - VLM_API_KEY                Bearer 鉴权
  - MEMORY_ENGINE_VLM_MODEL    视觉模型名（如 qwen3-vl-*）
  - MEMORY_ENGINE_VLM_TIMEOUT  请求超时秒（默认 60）

降级契约（P0 拍板）：未配置或调用失败 → describe_image() 返回 None，
调用方（api_attachments）将附件 state 置 'no_desc'、description 留空，上传不阻塞，仅 log.warning。

显存影响：VLM 走远程 HTTP，daemon 进程零显存、零模型常驻（本地 GPU 常驻的只有文本 embedder）。
"""
import json
import logging
import os
import urllib.request

log = logging.getLogger("memory-engine.vlm")

_PROMPT = "请用不超过80字的中文，客观描述这张图片的主要内容。"


def _cfg() -> tuple[str, str, str]:
    return (
        os.environ.get("VLM_BASE_URL", "").strip(),
        os.environ.get("VLM_API_KEY", ""),
        os.environ.get("MEMORY_ENGINE_VLM_MODEL", "").strip(),
    )


def configured() -> bool:
    base, _, model = _cfg()
    return bool(base and model)


def describe_image(image_b64: str, mime: str) -> str | None:
    """图片 base64 → 中文描述；未配置/失败一律返回 None（不抛出、不阻塞上传）。"""
    base, api_key, model = _cfg()
    if not (base and model):
        log.warning("vlm 未配置（VLM_BASE_URL/MEMORY_ENGINE_VLM_MODEL 缺失）→ 描述跳过，state=no_desc")
        return None
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
            ],
        }],
        "max_tokens": 256,
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=float(os.environ.get("MEMORY_ENGINE_VLM_TIMEOUT", "60"))) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        text = content.strip() if isinstance(content, str) else ""
        return text or None
    except Exception as e:  # noqa: BLE001 —— 降级契约：任何失败都不阻塞上传
        log.warning("VLM 描述失败（不阻塞上传，state=no_desc）: %s", str(e)[:200])
        return None

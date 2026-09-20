"""下载 Qwen3-Embedding-0.6B 到 MEMORY_ENGINE_MODEL_DIR（权重公开但随仓分发，README/CONTRIBUTING 承诺的入口）。

用法：python -m memory_engine.download_model
大陆网络：export HF_ENDPOINT=https://hf-mirror.com（默认走官方 endpoint）。
xet 协议与镜像不兼容，固定禁用（与生产 systemd 单元一致）。
"""
import os
import sys

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.pop("HF_HUB_OFFLINE", None)

from huggingface_hub import snapshot_download  # noqa: E402

from . import config  # noqa: E402  (hub env flags must precede huggingface_hub import)

PATTERNS = [
    "config.json", "generation_config.json", "model.safetensors",
    "tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt",
    "special_tokens_map.json", "added_tokens.json", "README.md",
]


def main() -> int:
    dest = str(config.MODEL_DIR)
    path = snapshot_download("Qwen/Qwen3-Embedding-0.6B",
                             local_dir=dest, allow_patterns=PATTERNS)
    n = sum(os.path.getsize(os.path.join(r, f))
            for r, _, fs in os.walk(path) for f in fs)
    print(f"MODEL_DOWNLOAD_OK {path} total_bytes={n / 1e6:.1f}MB", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

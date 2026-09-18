"""LLM 适配层：Anthropic Messages 协议，文本/多模态两模型。

端点与鉴权已实测通过（见 _plans 阶段一报告）。用量与耗时返回给调用方记账。
密钥从环境变量读，绝不出现在日志或异常信息里。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import httpx

import config as config_mod


@dataclass
class LLMResult:
    text: str
    model: str
    tokens_in: int
    tokens_out: int
    elapsed_ms: int


class LLMError(Exception):
    """分类后的调用错误。kind: auth / rate_limit / quota / timeout / http / other"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class LLM:
    def __init__(self, cfg: config_mod.Config):
        self.cfg = cfg
        llm = cfg.llm
        self.base_url = llm.base_url.rstrip("/")
        self.text_model = llm.text_model
        self.vision_model = llm.vision_model
        self.max_tokens = llm.max_tokens
        self.timeout = llm.timeout_seconds
        self._api_key = llm.api_key

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def complete(
        self,
        messages: list[dict],
        *,
        vision: bool = False,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> LLMResult:
        """发一次 Messages 请求。messages 为 Anthropic 格式。

        content 里的 image block 走 vision 模型；纯文本走 text 模型。
        """
        if not self._api_key:
            raise LLMError("auth", "缺少 API key（环境变量未设置）")

        model = self.vision_model if vision else self.text_model
        body: dict = {
            "model": model,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": messages,
        }
        if system:
            body["system"] = system

        url = f"{self.base_url}/v1/messages"
        start = time.monotonic()
        try:
            resp = httpx.post(
                url, headers=self._headers(), json=body, timeout=self.timeout
            )
        except httpx.TimeoutException as e:
            raise LLMError("timeout", f"调用超时: {model}") from e
        except httpx.HTTPError as e:
            raise LLMError("http", f"网络错误: {e}") from e

        if resp.status_code != 200:
            raise self._classify_error(resp.status_code, resp.text)

        data = resp.json()
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
        usage = data.get("usage", {})
        elapsed = int((time.monotonic() - start) * 1000)
        return LLMResult(
            text=text,
            model=model,
            tokens_in=usage.get("input_tokens", 0),
            tokens_out=usage.get("output_tokens", 0),
            elapsed_ms=elapsed,
        )

    @staticmethod
    def _classify_error(status: int, body: str) -> LLMError:
        snippet = body[:300]
        if status == 401 or status == 403:
            return LLMError("auth", f"鉴权失败 HTTP {status}")
        if status == 429:
            return LLMError("rate_limit", f"限流 HTTP 429: {snippet}")
        if status in (402, 402):
            return LLMError("quota", f"额度不足: {snippet}")
        # 尝试从响应体里抓错误类型，避免泄露 key（body 一般不含 key）
        if "insufficient" in body.lower() or "balance" in body.lower():
            return LLMError("quota", f"额度相关错误: {snippet}")
        return LLMError("other", f"HTTP {status}: {snippet}")

    def understand(self, text: str, system: str | None = None) -> LLMResult:
        """资料理解：摘要 + 关键事实 + 引用。"""
        return self.complete([{"role": "user", "content": text}], system=system)

    def describe_image(self, image_b64: str, mime: str, prompt: str | None = None) -> LLMResult:
        """多模态看图：图片交 vision 模型。"""
        content = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": image_b64},
            },
            {"type": "text", "text": prompt or "请详细描述这张图片的内容。"},
        ]
        return self.complete([{"role": "user", "content": content}], vision=True)

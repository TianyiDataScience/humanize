from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from copaw.providers.provider_manager import ProviderManager


def _extract_from_blocks(blocks: list[dict[str, Any]]) -> tuple[str, str]:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    for block in blocks:
        block_type = str(block.get("type") or "")
        if block_type == "text":
            value = str(block.get("text") or "")
            if value:
                text_parts.append(value)
        elif block_type == "thinking":
            value = str(block.get("thinking") or "")
            if value:
                thinking_parts.append(value)
    return "".join(text_parts).strip(), "\n".join(thinking_parts).strip()


async def _collect_stream_text(response: Any) -> tuple[str, str]:
    final_text = ""
    final_thinking = ""
    async for chunk in response:
        content = getattr(chunk, "content", None) or []
        if isinstance(content, list):
            text, thinking = _extract_from_blocks(content)
            if text:
                final_text = text
            if thinking:
                final_thinking = thinking
    return final_text, final_thinking


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    manager = ProviderManager.get_instance()
    active = manager.get_active_model()
    if not active:
        raise RuntimeError("No active CoPaw model configured")

    if args.probe:
        return {
            "provider_id": active.provider_id,
            "model": active.model,
            "backend": "copaw-active",
        }

    model = ProviderManager.get_active_chat_model()
    messages = [
        {"role": "system", "content": args.system_prompt},
        {"role": "user", "content": args.user_prompt},
    ]
    response = await model(
        messages,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )

    if hasattr(response, "__aiter__"):
        text, thinking = await _collect_stream_text(response)
    else:
        content = getattr(response, "content", None) or []
        if isinstance(content, list):
            text, thinking = _extract_from_blocks(content)
        else:
            text = str(getattr(response, "text", "") or "")
            thinking = ""

    return {
        "provider_id": active.provider_id,
        "model": active.model,
        "backend": "copaw-active",
        "text": text,
        "thinking": thinking,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate text with CoPaw's active chat model.",
    )
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--system-prompt", default="")
    parser.add_argument("--user-prompt", default="")
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--max-tokens", type=int, default=300)
    args = parser.parse_args()
    payload = asyncio.run(_run(args))
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()

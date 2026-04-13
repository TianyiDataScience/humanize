from __future__ import annotations

import argparse
import asyncio
import json
import urllib.request
from typing import Any

try:
    from qwenpaw.providers.models import ModelSlotConfig
    from qwenpaw.providers.provider_manager import ProviderManager
except ModuleNotFoundError:
    from copaw.providers.models import ModelSlotConfig
    from copaw.providers.provider_manager import ProviderManager

LOCAL_PROVIDER_ALIASES = ("copaw-local", "qwenpaw-local")


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


def _alias_candidates(provider_id: str) -> list[str]:
    candidates = [provider_id]
    if provider_id in LOCAL_PROVIDER_ALIASES:
        candidates.extend(
            alias for alias in LOCAL_PROVIDER_ALIASES if alias != provider_id
        )
    return candidates


def _sync_local_provider_alias(
    manager: ProviderManager,
    source_id: str,
    target_id: str,
) -> None:
    if source_id == target_id:
        return
    source_path = manager.builtin_path / f"{source_id}.json"
    if not source_path.exists():
        return
    with open(source_path, "r", encoding="utf-8") as handle:
        snapshot = json.load(handle)
    payload: dict[str, Any] = {}
    for key in ("base_url", "extra_models", "generate_kwargs"):
        value = snapshot.get(key)
        if value:
            payload[key] = value
    if payload:
        manager.update_provider(target_id, payload)


def _ensure_model_registered(
    manager: ProviderManager,
    provider_id: str,
    model_id: str,
) -> None:
    provider = manager.get_provider(provider_id)
    if provider is None or provider.has_model(model_id):
        return
    extra_models = [model.model_dump() for model in provider.extra_models]
    extra_models.append({"id": model_id, "name": model_id})
    manager.update_provider(provider_id, {"extra_models": extra_models})


def _assert_local_provider_healthy(manager: ProviderManager, provider_id: str) -> None:
    provider = manager.get_provider(provider_id)
    if provider is None:
        return
    is_local = bool(getattr(provider, "is_local", False)) or provider_id in LOCAL_PROVIDER_ALIASES
    if not is_local:
        return
    base_url = str(getattr(provider, "base_url", "") or "").rstrip("/")
    if not base_url:
        return
    health_base = base_url[:-3] if base_url.endswith("/v1") else base_url
    health_url = health_base.rstrip("/") + "/health"
    with urllib.request.urlopen(health_url, timeout=2.0) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("status") != "ok":
        raise RuntimeError(f"Active local model endpoint is not healthy at {base_url}: {payload}")


def _resolve_active_model(manager: ProviderManager) -> ModelSlotConfig:
    active = manager.get_active_model()
    if not active:
        raise RuntimeError("No active CoPaw model configured")

    candidates = _alias_candidates(active.provider_id)
    target_id = next(
        (provider_id for provider_id in candidates if manager.get_provider(provider_id)),
        active.provider_id,
    )
    source_id = next(
        (
            provider_id
            for provider_id in candidates
            if (manager.builtin_path / f"{provider_id}.json").exists()
        ),
        active.provider_id,
    )

    if target_id != source_id:
        _sync_local_provider_alias(manager, source_id, target_id)

    _ensure_model_registered(manager, target_id, active.model)

    if target_id != active.provider_id:
        active = ModelSlotConfig(provider_id=target_id, model=active.model)
        manager.active_model = active
        manager.save_active_model(active)

    provider = manager.get_provider(active.provider_id)
    if provider is None:
        raise RuntimeError(f"Active provider '{active.provider_id}' not found")
    if not provider.has_model(active.model):
        raise RuntimeError(
            f"Active model '{active.model}' not found in provider '{active.provider_id}'",
        )
    return active


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    manager = ProviderManager.get_instance()
    active = _resolve_active_model(manager)

    if args.probe:
        _assert_local_provider_healthy(manager, active.provider_id)
        ProviderManager.get_active_chat_model()
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

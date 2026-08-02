"""Configuration loading from .env (or environment variables)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str
    model: str
    timeout: int = 120
    max_tool_iters: int = 6
    max_repairs: int = 1


def load_settings(env_path: Path | None = None) -> Settings:
    """Load settings. Precedence: real environment variables > .env file.

    Raises ConfigError early (at startup, not mid-run) when the API key is missing.
    """
    load_dotenv(env_path)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ConfigError(
            "OPENAI_API_KEY is not configured: create a .env file in the project "
            "root (see .env.example) or set the OPENAI_API_KEY environment variable."
        )
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1").strip().rstrip("/")
    model = os.getenv("OPENAI_MODEL", "deepseek-chat").strip()
    try:
        timeout = int(os.getenv("OPENAI_TIMEOUT", "120"))
        max_tool_iters = int(os.getenv("MAX_TOOL_ITERS", "6"))
        max_repairs = int(os.getenv("MAX_REPAIRS", "1"))
    except ValueError as exc:
        raise ConfigError(f"invalid numeric setting in environment/.env: {exc}") from exc
    return Settings(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=timeout,
        max_tool_iters=max_tool_iters,
        max_repairs=max_repairs,
    )

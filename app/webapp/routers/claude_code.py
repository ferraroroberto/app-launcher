"""Claude-code config-shaped endpoint: launch-flag preview.

A small read of webapp_config (the `claude` subtree of /api/config),
surfaced on its own path for the options card's flag preview.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Request

from src.webapp_config import (
    ALWAYS_ON_CLAUDE_FLAGS,
    VALID_CLAUDE_EFFORTS,
    VALID_CLAUDE_MODELS,
    WebappConfig,
    build_claude_flags,
)

router = APIRouter()


@router.get("/api/claude-code/flags")
async def claude_flags(request: Request) -> Dict[str, Any]:
    cfg: WebappConfig = request.app.state.webapp_config
    return {
        "model": cfg.claude_model,
        "effort": cfg.claude_effort,
        "verbose": cfg.claude_verbose,
        "debug": cfg.claude_debug,
        "models_available": list(VALID_CLAUDE_MODELS),
        "efforts_available": list(VALID_CLAUDE_EFFORTS),
        "always_on_flags": list(ALWAYS_ON_CLAUDE_FLAGS),
        "computed_flags": build_claude_flags(cfg),
    }

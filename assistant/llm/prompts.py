"""Load and render the editable prompts in prompts/.

Prompts are Markdown files. Placeholders use the {{NAME}} form (NOT str.format,
so prompt text can freely contain braces/JSON examples). All prompts live in
prompts/ so you can edit tone and policy without touching code.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_CACHE: dict[str, str] = {}
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")

# Prompts used by the reasoning pipeline — the set we version for replay (Phase 3).
PIPELINE_PROMPTS = ("noise_filter", "think", "classifier", "self_critique")


def load(name: str, prompts_dir: str = "./prompts") -> str:
    """Read prompts/<name>.md (cached). Raises FileNotFoundError if missing."""
    key = f"{prompts_dir}::{name}"
    if key not in _CACHE:
        path = Path(prompts_dir) / f"{name}.md"
        _CACHE[key] = path.read_text(encoding="utf-8")
    return _CACHE[key]


def render(template: str, **values: object) -> str:
    """Replace {{NAME}} placeholders. Unknown placeholders are left intact."""
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(values[key]) if key in values else match.group(0)

    return _PLACEHOLDER.sub(repl, template)


def load_and_render(name: str, prompts_dir: str = "./prompts", **values: object) -> str:
    return render(load(name, prompts_dir), **values)


def clear_cache() -> None:
    _CACHE.clear()


def prompt_hash(name: str, prompts_dir: str = "./prompts") -> str:
    """Short content hash (sha256[:12]) of a prompt's current bytes — the prompt VERSION
    for replay/attribution (Phase 3). '' if the prompt is missing (best-effort)."""
    try:
        return hashlib.sha256(load(name, prompts_dir).encode("utf-8")).hexdigest()[:12]
    except Exception:  # noqa: BLE001
        return ""


def pipeline_versions(prompts_dir: str = "./prompts") -> dict[str, str]:
    """{prompt_name: version_hash} for the reasoning-pipeline prompts (Phase 3)."""
    return {n: prompt_hash(n, prompts_dir) for n in PIPELINE_PROMPTS}

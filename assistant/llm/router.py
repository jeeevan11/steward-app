"""TaskRouter — every LLM call names a TASK; the router picks the model + reasoning.

No caller passes a model string directly anymore; they pass a task. This gives one
place to tune the speed/quality/cost tradeoff per task, and one place that logs
task → model → tokens → cost (which feeds the dashboard's cost/model views).

Routing philosophy (the "hybrid" — smart where it matters, fast/cheap elsewhere):
  - Cheap, frequent, low-stakes work runs on Gemini Flash.
  - Only the genuinely consequential path (JUDGE_CRITICAL, DRAFT_CRITICAL,
    PATTERN_DETECT) spends Gemini Pro + a larger reasoning budget.
  - DeepSeek writes drafts (its prose) and voice profiles.

"Thinking budget" is expressed via OpenRouter's `reasoning` control (we're on the
OpenAI-compatible OpenRouter API, not the native Gemini/Anthropic SDKs), so the
budget numbers below are reasoning-token hints, applied best-effort.

Every task has a fallback model (Flash). On a hard failure of the primary model the
client retries ONCE against spec.fallback_model when it differs (llm-layer-1), and for
the consequential CRITICAL tasks (surface_on_fallback) it records the fallback in the
metrics ledger so the degradation is never silent — visible in the cost view and the
log, never an invisible quality drop.

Stdlib only — pure and unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass


class Task:
    NOISE_FILTER = "NOISE_FILTER"
    THINK = "THINK"
    JUDGE = "JUDGE"
    JUDGE_CRITICAL = "JUDGE_CRITICAL"
    SELF_CRITIQUE = "SELF_CRITIQUE"
    DRAFT = "DRAFT"
    DRAFT_CRITICAL = "DRAFT_CRITICAL"
    COMMITMENT_EXTRACT = "COMMITMENT_EXTRACT"
    VOICE_PROFILE = "VOICE_PROFILE"
    PATTERN_DETECT = "PATTERN_DETECT"
    QUALITY_CHECK = "QUALITY_CHECK"
    DISTILL = "DISTILL"
    # llm-layer-5 / llm-layer-6: explicit labels so media + free-text spend is
    # attributable in the per-task cost view instead of collapsing into task "?".
    TRANSCRIBE = "TRANSCRIBE"
    DESCRIBE_IMAGE = "DESCRIBE_IMAGE"
    TEXT = "TEXT"               # generic complete_text fallback label (briefs/commands/summaries)
    ALL = (
        NOISE_FILTER, THINK, JUDGE, JUDGE_CRITICAL, SELF_CRITIQUE, DRAFT,
        DRAFT_CRITICAL, COMMITMENT_EXTRACT, VOICE_PROFILE, PATTERN_DETECT, QUALITY_CHECK,
        DISTILL, TRANSCRIBE, DESCRIBE_IMAGE, TEXT,
    )


# Tasks whose failure must be surfaced to the human (never silently degraded).
CRITICAL_TASKS = frozenset({Task.JUDGE, Task.JUDGE_CRITICAL, Task.DRAFT_CRITICAL})


@dataclass(frozen=True)
class RouteSpec:
    task: str
    model: str
    thinking: bool
    reasoning_tokens: int          # 0 = thinking off
    fallback_model: str
    surface_on_fallback: bool

    # llm-layer-1 ROOT CAUSE: fallback_model / surface_on_fallback were documented
    # (router.py:17-19) and populated, but NO production code ever read them — _chat's
    # degrade loop only dropped reasoning/JSON and re-fired against the SAME model, then
    # raised LLMError. So a model-specific OpenRouter outage (e.g. Pro down, Flash up)
    # wedged EVERY critical-tier call into fail-safe even though a healthy cheaper model
    # was available and the docstring promised it would be used. These helpers make the
    # documented behavior real and let the client consult the spec instead of guessing.
    def has_fallback(self) -> bool:
        """True when a distinct fallback model is worth trying after the primary fails."""
        return bool(self.fallback_model) and self.fallback_model != self.model


# Approximate OpenRouter prices, USD per 1M tokens (input, output). Estimates only —
# used for the dashboard's cost view, not billing.
PRICES: dict[str, tuple[float, float]] = {
    "google/gemini-2.5-flash": (0.30, 2.50),
    "google/gemini-2.5-pro": (1.25, 10.00),
    "deepseek/deepseek-chat": (0.27, 1.10),
}
_DEFAULT_PRICE = (0.50, 3.00)


class TaskRouter:
    def __init__(self, settings):  # type: ignore[no-untyped-def]
        self.settings = settings
        flash = getattr(settings, "noise_model", "google/gemini-2.5-flash") or "google/gemini-2.5-flash"
        # judge_model is the default "smart-but-fast" model (Flash today).
        judge_flash = getattr(settings, "judge_model", flash) or flash
        pro = getattr(settings, "pro_model", "google/gemini-2.5-pro") or "google/gemini-2.5-pro"
        draft = getattr(settings, "draft_model", "deepseek/deepseek-chat") or "deepseek/deepseek-chat"
        self._flash = flash
        self._pro = pro

        # task -> (model, thinking, reasoning_tokens)
        self._map: dict[str, tuple[str, bool, int]] = {
            Task.NOISE_FILTER: (flash, False, 0),
            Task.THINK: (judge_flash, True, 1024),
            Task.JUDGE: (judge_flash, True, 2048),          # hybrid: Flash, not Pro
            Task.JUDGE_CRITICAL: (pro, True, 8192),         # Pro only on the critical path
            Task.SELF_CRITIQUE: (flash, False, 0),
            Task.DRAFT: (draft, False, 0),
            Task.DRAFT_CRITICAL: (pro, True, 4096),
            Task.COMMITMENT_EXTRACT: (flash, False, 0),
            Task.VOICE_PROFILE: (draft, False, 0),
            Task.PATTERN_DETECT: (pro, True, 2048),
            Task.QUALITY_CHECK: (flash, False, 0),
            Task.DISTILL: (flash, False, 0),   # extract-and-update memory; cheap + frequent
            # llm-layer-5/6: media + generic text. Model is overridden by the caller
            # (transcribe/describe use whatsapp_transcribe_model; complete_text passes its
            # own model), but mapping them keeps the cost view honest and documents intent.
            Task.TRANSCRIBE: (flash, False, 0),
            Task.DESCRIBE_IMAGE: (flash, False, 0),
            Task.TEXT: (judge_flash, False, 0),
        }

    def resolve(self, task: str) -> RouteSpec:
        model, thinking, budget = self._map.get(task, (self._flash, False, 0))
        return RouteSpec(
            task=task,
            model=model,
            thinking=thinking,
            reasoning_tokens=budget,
            fallback_model=self._flash,
            surface_on_fallback=task in CRITICAL_TASKS,
        )

    @staticmethod
    def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
        pin, pout = PRICES.get(model, _DEFAULT_PRICE)
        return (prompt_tokens / 1_000_000.0) * pin + (completion_tokens / 1_000_000.0) * pout

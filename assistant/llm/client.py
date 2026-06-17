"""LLM wrapper — talks to OpenRouter (an OpenAI-compatible gateway).

Two model roles, configurable in .env:
  * noise_model  (cheap, fast) — the "is this just noise?" first pass.   Default: google/gemini-2.5-flash
  * judge_model  (stronger)    — judgment + drafting in your voice.       Default: deepseek/deepseek-chat

The rest of the system only depends on the METHOD SIGNATURES here
(noise_pass / classify / draft / complete_text), not on the provider — so the
provider can be swapped again later by reimplementing just this file.

JSON calls use the model's JSON mode plus the schema embedded in the prompt; the
brain still validates every response defensively (brain/schema.py) and fails safe
to "ask the human" on anything malformed.

The `openai` package is imported lazily so importing the core never requires it.
"""

from __future__ import annotations

import os
import time as _time
import json
import threading
from typing import Any, Optional

from assistant.config import Settings
from assistant.llm.router import CRITICAL_TASKS, Task, TaskRouter
from assistant.logging_setup import get_logger

log = get_logger("llm")

# Completion headroom reserved for the JSON answer ON TOP OF the reasoning budget
# (see _chat). Without it, reasoning-enabled calls truncate their output.
_CONTENT_HEADROOM = 1024

# ── llm-layer-4 guard rails (spend cap + 429 circuit breaker + media byte cap) ──
# ROOT CAUSE: the LLM layer had NO ceiling. The only network handling was the SDK's
# max_retries=2, so a 429 became a tight retry storm; no code read accrued cost back to
# halt spend; and media (transcribe/describe_image) streamed raw base64 with no byte cap.
# On a live MODE=live account a runaway loop or an adversarial sender (the system already
# handles ~1500 msg/day) becomes an uncapped OpenRouter bill + an availability hit.
#
# We read the limits from os.environ with safe defaults (the shared config.py is off
# limits for this cluster — see schema_or_config_needed). All guards FAIL SAFE: when the
# ceiling is hit we raise LLMError, which every caller already turns into an owner
# heads-up + fail-safe, never an auto-send and never a silent drop.
def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        return int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


# Defaults are generous so they never bite a normal day, only a runaway/abuse case.
_DAILY_SPEND_CAP_USD = _env_float("LLM_DAILY_SPEND_CAP_USD", 25.0)   # 0 / negative = disabled
_BREAKER_THRESHOLD = _env_int("LLM_RATE_LIMIT_BREAKER_THRESHOLD", 5)  # consecutive 429s to trip
_BREAKER_COOLDOWN_S = _env_int("LLM_RATE_LIMIT_BREAKER_COOLDOWN_S", 60)
_MEDIA_MAX_BYTES = _env_int("LLM_MEDIA_MAX_BYTES", 12 * 1024 * 1024)  # decoded payload cap (~12 MB)


class _SpendBreakerGuard:
    """Process-wide rolling daily spend accumulator + consecutive-429 circuit breaker.

    Thread-safe (the receiver runs a thread per request; the bot has a single worker, but
    media + chat can interleave). Pure in-memory and best-effort — it never persists, so a
    restart resets the window; that is intentional (the cap is a runaway-cost backstop, not
    an accounting ledger — metrics.py remains the source of truth for spend reporting)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day = ""               # YYYY-MM-DD bucket for the spend window
        self._spend = 0.0
        self._consecutive_429 = 0
        self._tripped_until = 0.0    # epoch seconds the breaker stays open until

    @staticmethod
    def _today() -> str:
        return _time.strftime("%Y-%m-%d", _time.gmtime())

    def check(self) -> None:
        """Raise LLMError if the breaker is open or the daily cap is already exceeded.
        Called at the TOP of every model call so we refuse BEFORE spending more."""
        now = _time.time()
        with self._lock:
            if self._tripped_until and now < self._tripped_until:
                wait = int(self._tripped_until - now)
                raise LLMError(
                    f"LLM rate-limit circuit breaker open ({wait}s left after "
                    f"{self._consecutive_429} consecutive 429s) — failing safe")
            today = self._today()
            if today != self._day:
                self._day, self._spend = today, 0.0  # new UTC day → reset window
            if _DAILY_SPEND_CAP_USD > 0 and self._spend >= _DAILY_SPEND_CAP_USD:
                raise LLMError(
                    f"LLM daily spend cap reached (${self._spend:.2f} >= "
                    f"${_DAILY_SPEND_CAP_USD:.2f}) — failing safe until UTC midnight")

    def add_cost(self, cost: float) -> None:
        with self._lock:
            today = self._today()
            if today != self._day:
                self._day, self._spend = today, 0.0
            self._spend += max(0.0, float(cost or 0.0))

    def on_success(self) -> None:
        with self._lock:
            self._consecutive_429 = 0
            self._tripped_until = 0.0

    def on_rate_limit(self) -> None:
        """Record a 429; trip the breaker after _BREAKER_THRESHOLD consecutive hits."""
        with self._lock:
            self._consecutive_429 += 1
            if self._consecutive_429 >= _BREAKER_THRESHOLD:
                self._tripped_until = _time.time() + _BREAKER_COOLDOWN_S
                log.warning(
                    "LLM circuit breaker TRIPPED after %d consecutive 429s — pausing "
                    "model calls for %ds", self._consecutive_429, _BREAKER_COOLDOWN_S)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"day": self._day, "spend": round(self._spend, 5),
                    "consecutive_429": self._consecutive_429,
                    "tripped_until": self._tripped_until}


def _looks_transient(exc: Exception) -> bool:
    """llm-layer-3: True when an exception is a TRANSIENT provider error (timeout / 5xx /
    429) rather than a parameter-rejection (e.g. 'reasoning'/'extra_body' unsupported).

    We must NOT treat a transient hiccup as 'reasoning rejected' and silently re-fire the
    same critical call with reasoning disabled — that degrades a consequential judgment
    with no signal. Detect by openai exception TYPE when the SDK is importable, falling
    back to status-code / substring sniffing so the logic still works with a fake client
    in tests (which raises plain Exceptions)."""
    try:
        import openai  # type: ignore
        if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError,
                            openai.RateLimitError, openai.InternalServerError)):
            return True
        if isinstance(exc, openai.BadRequestError):
            return False  # 400 — a real parameter rejection, degrade is appropriate
    except Exception:  # noqa: BLE001 - SDK not importable (tests); fall through to sniffing
        pass
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        if status is not None and int(status) in (408, 429, 500, 502, 503, 504):
            return True
        if status is not None and int(status) in (400, 401, 403, 404, 422):
            return False
    except (TypeError, ValueError):
        pass
    text = str(exc).lower()
    if any(s in text for s in ("timeout", "timed out", "rate limit", "429",
                               "502", "503", "504", "overloaded", "temporarily")):
        return True
    return False


def _is_rate_limit(exc: Exception) -> bool:
    try:
        import openai  # type: ignore
        if isinstance(exc, openai.RateLimitError):
            return True
    except Exception:  # noqa: BLE001
        pass
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        if status is not None and int(status) == 429:
            return True
    except (TypeError, ValueError):
        pass
    return "rate limit" in str(exc).lower() or "429" in str(exc)


class LLMError(RuntimeError):
    """Model call failed after retries. Callers must treat this as 'uncertain' and
    fail safe (surface to the human), never act."""


class LLMClient:
    def __init__(self, settings: Settings, metrics_sink=None):
        self.settings = settings
        self.router = TaskRouter(settings)
        self.metrics_sink = metrics_sink  # optional callable(record: dict) for cost logging
        self._client = None  # lazily constructed OpenAI() pointed at OpenRouter
        # llm-layer-4: one guard per client instance (spend cap + 429 breaker). Shared by
        # every call this client makes (_chat AND the media paths).
        self._guard = _SpendBreakerGuard()

    # -- lazy SDK client ------------------------------------------------------
    def _api(self):  # type: ignore[no-untyped-def]
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover
                raise LLMError(
                    "The 'openai' package is not installed. Run: pip install -r requirements.txt"
                ) from exc
            # Bound every LLM call: without an explicit timeout the SDK default read
            # timeout is 600s, and these calls run on the bot's single-worker executor —
            # a black-holed OpenRouter connection would freeze every Telegram tap. Cap at
            # 60s/call with 2 retries so a hang fails fast instead of wedging the engine.
            self._client = OpenAI(
                api_key=self.settings.api_key,        # provider-neutral (LLM_API_KEY or OPENROUTER_API_KEY)
                base_url=self.settings.base_url,       # LLM_BASE_URL or OPENROUTER_BASE_URL
                timeout=60.0,
                max_retries=2,
            )
        return self._client

    @property
    def _extra_headers(self) -> dict:
        """OpenRouter-only attribution/ranking headers. Sent only when the provider is
        OpenRouter; other gateways (OpenAI, Together, Groq, local) get none — harmless either
        way, but cleaner to omit. Returns {} for non-OpenRouter providers."""
        if (self.settings.llm_provider or "openrouter") != "openrouter":
            return {}
        return {"HTTP-Referer": "http://localhost", "X-Title": "Steward"}

    # -- low-level chat -------------------------------------------------------
    def _chat(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool = False,
        reasoning_tokens: int = 0,
        task: str = "",
        message_id: str = "",
    ) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "extra_headers": self._extra_headers,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if reasoning_tokens > 0:
            # OpenRouter reasoning control (we're not on the native Gemini SDK).
            kwargs["extra_body"] = {"reasoning": {"max_tokens": reasoning_tokens}}
            # Reasoning tokens consume the completion budget, so a max_tokens that is
            # at or below the reasoning budget leaves no room for the actual JSON and
            # the model truncates mid-structure (observed: flash JUDGE returning an
            # unterminated string). Guarantee headroom for the answer on top of the
            # reasoning. This is a CAP, not a target, so it doesn't raise cost unless
            # output was being cut off (which we want to fix).
            kwargs["max_tokens"] = max(max_tokens, reasoning_tokens + _CONTENT_HEADROOM)

        # llm-layer-4: refuse BEFORE spending if the daily cap is hit or the 429 breaker
        # is open. Raises LLMError → caller fails safe (no auto-send, no silent drop).
        self._guard.check()

        def _call(kw):
            return self._api().chat.completions.create(**kw)

        # llm-layer-1: figure out the fallback model up front so a hard primary failure
        # can degrade to a healthy cheaper model instead of wedging into fail-safe.
        spec = self.router.resolve(task) if task else None
        fallback_model = spec.fallback_model if (spec and spec.has_fallback()) else ""
        is_critical = task in CRITICAL_TASKS

        resp = None
        used_model = model
        used_fallback = False
        # Attempt ladder: try the primary, then (only for genuine PARAMETER rejections)
        # drop reasoning, then drop JSON. A TRANSIENT error (timeout/5xx/429) is NOT a
        # parameter rejection — we must not silently strip reasoning on it (llm-layer-3).
        for attempt in ("full", "no_reasoning", "no_json"):
            try:
                resp = _call(kwargs)
                break
            except Exception as exc:  # noqa: BLE001
                # llm-layer-3: only DEGRADE the request shape on a real parameter
                # rejection. A transient hiccup keeps the full request (incl. reasoning)
                # and breaks out of the degrade ladder so we don't run a critical call at
                # silently-reduced quality.
                transient = _looks_transient(exc)
                if _is_rate_limit(exc):
                    self._guard.on_rate_limit()
                if not transient and "extra_body" in kwargs and attempt == "full":
                    log.warning("reasoning param rejected by %s (%s); retrying without it",
                                model, exc)
                    kwargs.pop("extra_body", None)
                    continue
                if not transient and json_mode and "response_format" in kwargs:
                    log.warning("JSON mode rejected by %s (%s); retrying without it", model, exc)
                    kwargs.pop("response_format", None)
                    continue
                # Either transient, or we've exhausted shape degradations. llm-layer-1:
                # try the fallback model ONCE (full request preserved) before failing safe.
                if fallback_model:
                    log.warning("model %s failed (%s); failing over to %s",
                                model, exc, fallback_model)
                    fb_kwargs = dict(kwargs)
                    fb_kwargs["model"] = fallback_model
                    try:
                        resp = _call(fb_kwargs)
                        used_model = fallback_model
                        used_fallback = True
                        break
                    except Exception as fexc:  # noqa: BLE001
                        if _is_rate_limit(fexc):
                            self._guard.on_rate_limit()
                        raise LLMError(
                            f"chat failed for {model} and fallback {fallback_model}: {fexc}"
                        ) from fexc
                raise LLMError(f"chat failed for {model}: {exc}") from exc

        self._guard.on_success()
        # llm-layer-1: when a CRITICAL task ran on the fallback model the degradation must
        # never be silent — record it in the metrics ledger (fallback=True) AND log loudly
        # so the cost-by-task view shows it. surface_on_fallback drives this signal.
        if used_fallback and is_critical:
            log.warning("CRITICAL task=%s ran on fallback model %s (primary %s failed) — "
                        "degraded, surfaced in metrics", task, used_model, model)
        self._log_usage(resp, model=used_model, task=task, message_id=message_id,
                        fallback=used_fallback)
        return (resp.choices[0].message.content or "").strip()

    def _log_usage(self, resp, *, model: str, task: str, message_id: str,
                   fallback: bool = False) -> None:
        """Log task/model/tokens/cost (python logger + optional metrics sink). Best-effort.

        llm-layer-4: also accumulate the estimated cost into the spend guard so the daily
        cap can halt runaway spend. llm-layer-1: thread `fallback` through to the sink so a
        degraded (fallback-model) critical call is visible in the cost view, never silent."""
        try:
            usage = getattr(resp, "usage", None)
            pt = int(getattr(usage, "prompt_tokens", 0) or 0)
            ct = int(getattr(usage, "completion_tokens", 0) or 0)
            cost = TaskRouter.estimate_cost(model, pt, ct)
            # Feed the rolling daily-spend guard (cap is checked at the top of the NEXT call).
            try:
                self._guard.add_cost(cost)
            except Exception:  # noqa: BLE001 - spend accounting must never break a call
                pass
            log.info("llm task=%s model=%s tok=%d/%d cost=$%.5f%s",
                     task or "?", model, pt, ct, cost, " [fallback]" if fallback else "")
            if self.metrics_sink is not None:
                self.metrics_sink({
                    "task": task, "model": model, "prompt_tokens": pt,
                    "completion_tokens": ct, "cost": cost, "message_id": message_id,
                    "fallback": fallback,
                })
        except Exception:  # noqa: BLE001 - logging must never break a call
            pass

    @staticmethod
    def _with_schema(system_prefix: str, schema: dict[str, Any]) -> str:
        """Append the exact target JSON shape so the model can't drift."""
        return (
            system_prefix
            + "\n\nReturn ONLY a single JSON object, no prose, matching exactly this "
            + "JSON schema (same keys and value types):\n"
            + json.dumps(schema)
        )

    # -- noise pass (cheap model) --------------------------------------------
    def noise_pass(self, *, system_prefix: str, thread_text: str, schema: dict[str, Any],
                   message_id: str = "") -> str:
        spec = self.router.resolve(Task.NOISE_FILTER)
        return self._chat(
            model=spec.model,
            system=self._with_schema(system_prefix, schema),
            user=thread_text,
            max_tokens=400,
            temperature=0.0,
            json_mode=True,
            reasoning_tokens=spec.reasoning_tokens,
            task=spec.task,
            message_id=message_id,
        )

    # -- generic task-routed JSON call (commitments, quality gate, …) --------
    def complete_json(self, *, task: str, system_prefix: str, user_text: str,
                      schema: dict[str, Any], max_tokens: int = 700, message_id: str = "") -> str:
        """Run a task-routed, schema-constrained JSON call. The caller parses + fails
        safe on bad output (we never trust the model blindly)."""
        spec = self.router.resolve(task)
        return self._chat(
            model=spec.model,
            system=self._with_schema(system_prefix, schema),
            user=user_text,
            max_tokens=max_tokens,
            temperature=0.0,
            json_mode=True,
            reasoning_tokens=spec.reasoning_tokens,
            task=spec.task,
            message_id=message_id,
        )

    # -- THINK: cheap prep pass before judgment (P3 step 1) ------------------
    def think(self, *, system_prefix: str, thread_text: str, schema: dict[str, Any],
              message_id: str = "") -> str:
        spec = self.router.resolve(Task.THINK)
        return self._chat(
            model=spec.model,
            system=self._with_schema(system_prefix, schema),
            user=thread_text,
            max_tokens=700,
            temperature=0.0,
            json_mode=True,
            reasoning_tokens=spec.reasoning_tokens,
            task=spec.task,
            message_id=message_id,
        )

    # -- SELF_CRITIQUE: re-check the judgment (P3 step 3) --------------------
    def self_critique(self, *, system_prefix: str, user_text: str, schema: dict[str, Any],
                      message_id: str = "") -> str:
        spec = self.router.resolve(Task.SELF_CRITIQUE)
        return self._chat(
            model=spec.model,
            system=self._with_schema(system_prefix, schema),
            user=user_text,
            max_tokens=300,
            temperature=0.0,
            json_mode=True,
            reasoning_tokens=spec.reasoning_tokens,
            task=spec.task,
            message_id=message_id,
        )

    # -- classification --------------------------------------------------------
    def classify(
        self, *, system_prefix: str, thread_text: str, schema: dict[str, Any],
        effort: str = "high", task: str = Task.JUDGE, message_id: str = "",
        reasoning_override: Optional[int] = None,
    ) -> str:
        # `effort` kept for signature compatibility. `task` lets the three-step
        # reasoning (P3) request JUDGE vs JUDGE_CRITICAL; default JUDGE.
        # `reasoning_override` forces a specific reasoning budget (0 = off) — used for
        # the no-reasoning retry when a reasoning-on JUDGE returns empty/unparseable.
        spec = self.router.resolve(task)
        rt = spec.reasoning_tokens if reasoning_override is None else reasoning_override
        return self._chat(
            model=spec.model,
            system=self._with_schema(system_prefix, schema),
            user=thread_text,
            max_tokens=1200,
            temperature=0.1,
            json_mode=True,
            reasoning_tokens=rt,
            task=spec.task,
            message_id=message_id,
        )

    # -- drafting (free text) --------------------------------------------------
    def draft(
        self, *, system_prefix: str, user_prompt: str, max_tokens: int = 1200,
        effort: str = "high", task: str = Task.DRAFT, message_id: str = "",
    ) -> str:
        spec = self.router.resolve(task)
        return self._chat(
            model=spec.model,
            system=system_prefix,
            user=user_prompt,
            max_tokens=max_tokens,
            temperature=0.4,
            json_mode=False,
            reasoning_tokens=spec.reasoning_tokens,
            task=spec.task,
            message_id=message_id,
        )

    # llm-layer-4/5 ROOT CAUSE: media (audio/image) base64 was streamed to the model with
    # NO byte cap anywhere, so a multi-megabyte voice note / high-res image became uncapped
    # input-token cost — and because transcribe/describe_image bypassed _chat, that cost was
    # never metered. This guard rejects oversized payloads BEFORE the network call so spam
    # media can't drive unbounded, unmetered spend. Decoded byte length ≈ len(b64)*3/4.
    @staticmethod
    def _media_too_large(b64: str) -> bool:
        if _MEDIA_MAX_BYTES <= 0:
            return False  # cap disabled
        approx_bytes = (len(b64 or "") * 3) // 4
        return approx_bytes > _MEDIA_MAX_BYTES

    # -- audio transcription (WhatsApp voice notes) --------------------------
    def transcribe(self, *, audio_b64: str, audio_format: str = "ogg",
                   model: Optional[str] = None, message_id: str = "") -> str:
        """Transcribe audio via a multimodal model on OpenRouter (best-effort).

        OpenRouter has no Whisper endpoint, so we send the audio as an `input_audio`
        content part to an audio-capable chat model (Gemini). Raises LLMError on any
        failure — the caller (whatsapp_source) falls back to a safe placeholder, never
        a fabricated transcript.

        llm-layer-4/5: cap input size, refuse via the spend/breaker guard before spending,
        and route token usage through _log_usage so media cost reaches the metrics sink.
        """
        model = model or self.settings.whatsapp_transcribe_model
        if self._media_too_large(audio_b64):
            raise LLMError(
                f"transcribe rejected: audio exceeds {_MEDIA_MAX_BYTES} byte cap "
                f"(~{(len(audio_b64) * 3) // 4} bytes)")
        self._guard.check()
        try:
            resp = self._api().chat.completions.create(
                model=model,
                max_tokens=1000,
                temperature=0.0,
                extra_headers=self._extra_headers,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Transcribe this voice note verbatim. "
                                                 "Output only the transcript, no preamble."},
                        {"type": "input_audio", "input_audio": {"data": audio_b64, "format": audio_format}},
                    ],
                }],
            )
            # llm-layer-5: media cost was invisible — meter it like every other call.
            self._guard.on_success()
            self._log_usage(resp, model=model, task=Task.TRANSCRIBE, message_id=message_id)
            return (resp.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001
            if _is_rate_limit(exc):
                self._guard.on_rate_limit()
            raise LLMError(f"transcribe failed for {model}: {exc}") from exc

    # GAP 8 — spec-named alias for transcribe (audio_b64 → transcript text).
    def transcribe_audio(self, audio_b64: str, audio_format: str = "ogg",
                         model: Optional[str] = None, message_id: str = "") -> Optional[str]:
        """GAP 8 — transcribe a voice note. Returns the transcript, or None on failure
        (so the caller can fall through to a safe placeholder rather than fabricate)."""
        if not audio_b64:
            return None
        try:
            return self.transcribe(audio_b64=audio_b64, audio_format=audio_format,
                                   model=model, message_id=message_id)
        except LLMError:
            return None

    # -- image description (WhatsApp images) ---------------------------------
    def describe_image(self, image_b64: str, *, image_format: str = "jpeg",
                       model: Optional[str] = None, message_id: str = "") -> Optional[str]:
        """GAP 8 — one-sentence description of an image via a vision-capable model.
        Returns the description, or None on any failure (caller falls back to '[image]').

        llm-layer-4/5: cap input size, refuse via the spend/breaker guard, and meter token
        usage through _log_usage so image cost reaches the metrics sink (it was invisible)."""
        if not image_b64:
            return None
        model = model or self.settings.whatsapp_transcribe_model
        if self._media_too_large(image_b64):
            log.warning("describe_image rejected: image exceeds %d byte cap (~%d bytes)",
                        _MEDIA_MAX_BYTES, (len(image_b64) * 3) // 4)
            return None
        try:
            self._guard.check()
        except LLMError as exc:
            log.warning("describe_image skipped by spend/breaker guard: %s", exc)
            return None
        # Accept either a bare base64 string or a full data URL.
        url = image_b64 if image_b64.startswith("data:") else \
            f"data:image/{image_format};base64,{image_b64}"
        try:
            resp = self._api().chat.completions.create(
                model=model,
                max_tokens=120,
                temperature=0.0,
                extra_headers=self._extra_headers,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image in one sentence for a "
                                                 "personal assistant context. Output only the "
                                                 "description, no preamble."},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }],
            )
            self._guard.on_success()
            self._log_usage(resp, model=model, task=Task.DESCRIBE_IMAGE, message_id=message_id)
            return (resp.choices[0].message.content or "").strip() or None
        except Exception as exc:  # noqa: BLE001 - vision is best-effort
            if _is_rate_limit(exc):
                self._guard.on_rate_limit()
            log.warning("describe_image failed for %s: %s", model, exc)
            return None

    # -- generic text (briefs, command parsing, voice summary) ---------------
    def complete_text(
        self,
        *,
        model: Optional[str] = None,
        system_prefix: str,
        user_prompt: str,
        max_tokens: int = 1200,
        use_opus: bool = True,
        effort: str = "medium",
        task: str = Task.TEXT,
        message_id: str = "",
    ) -> str:
        # llm-layer-6 ROOT CAUSE: complete_text called _chat with NO task/message_id, so
        # every brief / command / voice-summary / distill / opportunity / compose call was
        # recorded under task "?" with an empty message_id — a non-trivial slice of daily
        # spend was unattributable in the cost-by-task view. Default to a non-empty TEXT
        # label and accept an explicit task= + message_id= so callers can attribute their
        # spend precisely. Additive: existing callers keep working and now log "TEXT"
        # instead of "?".
        chosen = model or (self.settings.judge_model if use_opus else self.settings.noise_model)
        return self._chat(
            model=chosen,
            system=system_prefix,
            user=user_prompt,
            max_tokens=max_tokens,
            temperature=0.3,
            json_mode=False,
            task=task or Task.TEXT,
            message_id=message_id,
        )

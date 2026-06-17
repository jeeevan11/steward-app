"""Central configuration.

Loads settings from environment / a local `.env`. Secrets live in `.env`, never in
code. This module depends only on the standard library (the `.env` parser is a tiny
fallback) so the testable core never needs third-party packages installed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _load_dotenv(path: str = ".env") -> None:
    """Populate os.environ from a .env file without requiring python-dotenv.

    If python-dotenv is installed we prefer it (handles quoting/escaping). Otherwise
    we fall back to a minimal KEY=VALUE parser. Existing env vars always win.
    """
    try:  # pragma: no cover - exercised only when dotenv is installed
        from dotenv import load_dotenv

        load_dotenv(path, override=False)
        return
    except Exception:
        pass

    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _get_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _get_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _get_csv(key: str, *, lower: bool = False) -> tuple[str, ...]:
    raw = os.environ.get(key, "") or ""
    items = [p.strip() for p in raw.split(",") if p.strip()]
    if lower:
        items = [p.lower() for p in items]
    return tuple(items)


@dataclass(frozen=True)
class Settings:
    # LLM — bring ANY OpenAI-compatible provider's key. Steward talks to the LLM through the
    # OpenAI SDK, so it works with OpenRouter (default), OpenAI, Together, Groq, a local
    # vLLM/Ollama server, etc. Pick one via env: set LLM_API_KEY + LLM_BASE_URL (+ the *_MODEL
    # ids for that provider). The OPENROUTER_* vars still work and are used as a fallback so
    # existing setups need no change. `llm_provider` only tweaks provider-specific niceties
    # (the OpenRouter ranking headers are sent only when provider == "openrouter").
    llm_provider: str = "openrouter"            # openrouter | openai | anthropic | other
    llm_api_key: str = ""
    llm_base_url: str = ""
    openrouter_api_key: str = ""                 # fallback / back-compat
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    judge_model: str = "google/gemini-2.5-flash"    # classification (runs on every email — keep fast)
    noise_model: str = "google/gemini-2.5-flash"    # cheap "is this noise?" pass
    draft_model: str = "google/gemini-2.5-flash"    # reply drafting (swap to deepseek/deepseek-chat for its style)
    pro_model: str = "google/gemini-2.5-pro"        # reserved for the critical path (router JUDGE_CRITICAL etc.)

    # Addresses that are the PRINCIPAL themselves — inbound from these is never
    # processed (e.g. Jatin's other inboxes). Comma-separated in .env.
    self_addresses: tuple[str, ...] = field(default_factory=tuple)

    # Gmail
    gmail_credentials_path: str = "./secrets/client_secret.json"
    gmail_token_path: str = "./secrets/gmail_token.json"
    gmail_address: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Behaviour
    mode: str = "dry_run"  # "dry_run" | "live"
    email_enabled: bool = True   # master toggle for the Gmail channel (Mac app / .env)
    poll_interval_seconds: int = 45
    surface_confidence_threshold: float = 0.75
    autonomy_confidence_threshold: float = 0.85
    vip_importance_threshold: int = 70

    # Gmail push notifications (P0a — opt-in). Empty topic = polling only (default).
    # The Pub/Sub topic looks like "projects/<proj>/topics/<topic>". The push
    # receiver binds 127.0.0.1:<port> and just WAKES the poller (the history fetch
    # + ledger dedup do the rest), so duplicate pushes are harmless.
    gmail_pubsub_topic: str = ""
    gmail_pubsub_port: int = 8001

    # NO_SILENT_LOSS: when Gmail's incremental historyId expires (any outage longer than
    # Gmail's retained-history window), Steward resyncs by rescanning recent inbox. This
    # is how many days back that rescan reaches. 1 day silently dropped weekend/outage
    # mail (finding `ingest-email-1`); 7 covers realistic gaps. Anything older than this
    # on a gap resync is surfaced to the owner, never silently skipped.
    gmail_resync_days: int = 7

    # Calendar context (P4 — opt-in). Enabling adds the calendar.readonly scope,
    # which forces a one-time re-consent on next run.
    calendar_enabled: bool = False

    # Commitment tracker (P4): hour (local) for the daily 8am commitment/stale sweep.
    commitment_check_hour: int = 8

    # Proactive chief-of-staff digest (Phase 9): once-daily curated summary of unanswered
    # important people, at-risk commitments, stalled threads, recurring requests.
    proactive_enabled: bool = True
    proactive_hour: int = 9

    # Draft quality gate (P5b): em-dash + filler auto-fix, fabrication/length flags.
    quality_gate_enabled: bool = True

    # GAP 3 — autonomous-send guardrail. When False (default), replies to personal
    # contacts (relationship_type partner/family) are HELD for explicit approval rather
    # than auto-sent, even if the rest of the pipeline would have sent them. Set True to
    # let personal replies send autonomously.
    personal_auto_send: bool = False

    # Cross-channel memory (Memory layer). memory_enabled is the master switch; if it
    # or any memory step fails, the brain falls through to thread-only classification.
    memory_enabled: bool = True
    link_suggestions_enabled: bool = True  # surface "same person?" merge prompts to Telegram
    memory_distill_enabled: bool = True    # run the after-interaction relationship distill
    memory_nudge_cooldown_hours: int = 24  # don't re-surface a skipped situation within this
    memory_governance_enabled: bool = True  # Phase 6: per-fact confidence/decay/verification

    # Shared secret for relay<->engine HTTP (INGEST_TOKEN). Empty = legacy localhost-only
    # behavior (fail open, but the engine + relay warn loudly in live mode). When set, it
    # is the X-Cos-Token shared secret on BOTH directions of the WhatsApp relay link:
    #   - relay -> engine: required on /inbound /outbound (and web write endpoints)
    #   - engine -> relay: attached on /send /read /send_media and required by the relay
    #     on /send /read /send_media /contacts /resolve-lid (config-secrets-deploy-1).
    # The relay reads the SAME INGEST_TOKEN env var. Backward-compatible (additive).
    ingest_token: str = ""
    console_token: str = ""

    # Reliability / retention (Phase 11).
    retention_enabled: bool = True
    retention_days: int = 90              # high-volume logs/metrics/decisions
    retention_wa_history_days: int = 30   # wa_messages context history (>= 14d context window)
    relay_health_alert_enabled: bool = True
    relay_stale_alert_seconds: int = 180  # alert if relay status is older/disconnected than this
    relay_status_path: str = "relay/status.json"
    stuck_send_minutes: int = 30          # a pending action stuck in SENDING longer than this is flagged

    # Storage / paths
    db_path: str = "./data/assistant.db"
    log_path: str = "./data/assistant.log"
    prompts_dir: str = "./prompts"

    # Briefs
    timezone: str = "America/Los_Angeles"
    morning_brief_hour: int = 8
    evening_brief_hour: int = 18

    # WhatsApp (Phase 2 — off by default). The Node relay talks to these ports.
    whatsapp_enabled: bool = False
    whatsapp_relay_port: int = 7999     # Python receiver listens here for /inbound
    whatsapp_send_port: int = 7998      # Node relay listens here for /send, /read
    personal_jids: tuple[str, ...] = field(default_factory=tuple)   # always Tier 3
    # Per-contact rules (Layer 1A). VIP = "always-instant": bypasses the settling
    # delay and is never quieted — you always hear about them. MUTE = "never": silently
    # handled, never pings you (but still clamped to the hard guardrail floor, so a
    # genuinely consequential message — money/legal/irreversible — still surfaces).
    vip_jids: tuple[str, ...] = field(default_factory=tuple)
    mute_jids: tuple[str, ...] = field(default_factory=tuple)
    watch_keywords: tuple[str, ...] = field(default_factory=tuple)  # group trigger words
    wa_user_jid: str = ""               # your own JID, for @mention detection in groups
    whatsapp_transcribe_model: str = "google/gemini-2.5-flash"  # audio-capable model

    # WhatsApp settling / debounce: people text line-by-line. Rather than fire a card
    # per line, a conversation's burst is HELD until it goes quiet, then processed as a
    # single thread → a single card. Time-based (durable in SQLite, so a restart loses
    # nothing). "settle" = seconds of silence before release; "max_hold" = a safety cap
    # so a slow trickle still surfaces. Groups wait far longer (they have natural lulls
    # and we never want to ping while a group is active). Disable → instant per-message.
    whatsapp_settle_enabled: bool = True
    whatsapp_settle_seconds: int = 75               # 1:1 chats: silence before release
    whatsapp_settle_max_hold_seconds: int = 900     # 1:1 cap (15 min) — never starved
    whatsapp_group_settle_seconds: int = 300        # groups: 5 min of quiet
    whatsapp_group_max_hold_seconds: int = 10800    # group cap (3 h)

    # Presence-aware suppression (Layer 1B). If the owner is handling a conversation
    # himself, don't ping him about it — but the agent STILL tracks everything.
    presence_suppression_enabled: bool = True
    presence_outbound_cooldown_seconds: int = 300   # he replied here in the last 5 min → silent
    presence_app_focus_enabled: bool = True         # native WhatsApp app frontmost → defer
    # Read-receipt quiet hours (Fix 2). During this LOCAL-time window the agent does NOT
    # send WhatsApp read receipts (blue ticks) when auto-handling a chat, so it never
    # signals "read at 3am". The window wraps midnight when start > end; start == end
    # disables it. Affects ONLY /read — outbound /send is never gated by this.
    read_receipt_quiet_hours_enabled: bool = True
    read_receipt_quiet_start_hour: int = 22   # 10pm local (settings.timezone)
    read_receipt_quiet_end_hour: int = 8      # 8am local
    # Rolling conversation context fed to the brain (Layer 1C).
    whatsapp_context_days: int = 14
    # Message-lifecycle "silence sweep": detect a 1:1 message that is "not going" (stuck
    # undelivered) or "not coming back" (delivered/read but unanswered, either direction) and
    # surface it as an INFORMATIONAL FYI (never a send card). Pure read; honors pause.
    whatsapp_silence_sweep_enabled: bool = True
    wa_stuck_secs: int = 120              # owner send stuck PENDING this long → "not going"
    wa_unanswered_out_secs: int = 21600   # they haven't replied to you in 6h (delivered)
    wa_unanswered_in_secs: int = 10800    # you haven't replied to them in 3h
    wa_silence_sweep_interval_secs: int = 1800   # surface at most this often (30 min)
    wa_silence_max_age_secs: int = 345600        # ignore silences older than 4 days (no burst)
    # Learn his WhatsApp talking style from his own sent messages (Layer 1D).
    whatsapp_style_enabled: bool = True
    # Feedback-loop tuning (Layer 1E): after this many skips with ~no approvals for a
    # sender, quietly lower how loudly we surface them (never below the guardrail floor).
    feedback_tuning_enabled: bool = True
    feedback_skip_threshold: int = 3

    # Mini App (optional — enables the Telegram Mini App surface).
    miniapp_url: str = ""
    # Full HTTPS URL of the Mini App. Empty = Mini App button disabled.
    miniapp_secret: str = ""
    # Secret for Mini App JWT signing. Defaults to bot token if empty.

    # LLM-based thread enrichment (opt-in features).
    opportunity_detection_enabled: bool = True
    # Enable LLM-based opportunity detection from processed threads.
    project_tagging_enabled: bool = True
    # Enable LLM-based project auto-tagging of threads.

    # Scheduling.
    state_update_hour: int = 7
    # Hour (24h) for the daily state update job.

    # Gmail scopes — modify covers read + label + send. Kept narrow on purpose.
    gmail_scopes: tuple[str, ...] = field(
        default=("https://www.googleapis.com/auth/gmail.modify",)
    )

    @property
    def dry_run(self) -> bool:
        return self.mode.strip().lower() != "live"

    def ensure_dirs(self) -> None:
        """Create the data/secrets/prompts parent dirs if missing (idempotent)."""
        for p in (self.db_path, self.log_path):
            Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(self.gmail_token_path).parent.mkdir(parents=True, exist_ok=True)

    @property
    def api_key(self) -> str:
        """The LLM key actually used — the provider-neutral LLM_API_KEY if set, else the
        OPENROUTER_API_KEY fallback (so existing setups keep working)."""
        return self.llm_api_key or self.openrouter_api_key

    @property
    def base_url(self) -> str:
        """The LLM endpoint actually used — LLM_BASE_URL if set, else OPENROUTER_BASE_URL."""
        return self.llm_base_url or self.openrouter_base_url

    def missing_required(self) -> list[str]:
        """Return human-readable names of required settings that are blank.

        Used at startup to fail loudly rather than silently mis-behaving.
        """
        missing = []
        if not self.api_key:
            missing.append("LLM_API_KEY (or OPENROUTER_API_KEY)")
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        if not Path(self.gmail_credentials_path).exists():
            missing.append(f"GMAIL_CREDENTIALS_PATH (file not found: {self.gmail_credentials_path})")
        return missing


_cached: Optional[Settings] = None


def load_settings(env_path: str = ".env", *, reload: bool = False) -> Settings:
    """Load settings once and cache. Pass reload=True to re-read (tests)."""
    global _cached
    if _cached is not None and not reload:
        return _cached
    _load_dotenv(env_path)
    # Gmail scope is modify by default; calendar.readonly is added only when the
    # calendar feature is enabled (it forces a one-time re-consent).
    calendar_enabled = _get_bool("CALENDAR_ENABLED", False)
    scopes = ["https://www.googleapis.com/auth/gmail.modify"]
    if calendar_enabled:
        scopes.append("https://www.googleapis.com/auth/calendar.readonly")
    _cached = Settings(
        llm_provider=_get("LLM_PROVIDER", "openrouter").strip().lower() or "openrouter",
        llm_api_key=_get("LLM_API_KEY"),
        llm_base_url=_get("LLM_BASE_URL"),
        openrouter_api_key=_get("OPENROUTER_API_KEY"),
        openrouter_base_url=_get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        judge_model=_get("JUDGE_MODEL", "google/gemini-2.5-flash"),
        noise_model=_get("NOISE_MODEL", "google/gemini-2.5-flash"),
        draft_model=_get("DRAFT_MODEL", "google/gemini-2.5-flash"),
        pro_model=_get("PRO_MODEL", "google/gemini-2.5-pro"),
        self_addresses=_get_csv("SELF_ADDRESSES", lower=True),
        gmail_credentials_path=_get("GMAIL_CREDENTIALS_PATH", "./secrets/client_secret.json"),
        gmail_token_path=_get("GMAIL_TOKEN_PATH", "./secrets/gmail_token.json"),
        gmail_address=_get("GMAIL_ADDRESS"),
        telegram_bot_token=_get("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_get("TELEGRAM_CHAT_ID"),
        mode=_get("MODE", "dry_run"),
        email_enabled=_get_bool("EMAIL_ENABLED", True),
        poll_interval_seconds=_get_int("POLL_INTERVAL_SECONDS", 45),
        surface_confidence_threshold=_get_float("SURFACE_CONFIDENCE_THRESHOLD", 0.75),
        autonomy_confidence_threshold=_get_float("AUTONOMY_CONFIDENCE_THRESHOLD", 0.85),
        vip_importance_threshold=_get_int("VIP_IMPORTANCE_THRESHOLD", 70),
        gmail_pubsub_topic=_get("GMAIL_PUBSUB_TOPIC"),
        gmail_pubsub_port=_get_int("GMAIL_PUBSUB_PORT", 8001),
        gmail_resync_days=_get_int("GMAIL_RESYNC_DAYS", 7),
        calendar_enabled=calendar_enabled,
        commitment_check_hour=_get_int("COMMITMENT_CHECK_HOUR", 8),
        proactive_enabled=_get_bool("PROACTIVE_ENABLED", True),
        proactive_hour=_get_int("PROACTIVE_HOUR", 9),
        quality_gate_enabled=_get_bool("QUALITY_GATE_ENABLED", True),
        personal_auto_send=_get_bool("PERSONAL_AUTO_SEND", False),
        memory_enabled=_get_bool("MEMORY_ENABLED", True),
        link_suggestions_enabled=_get_bool("LINK_SUGGESTIONS_ENABLED", True),
        memory_distill_enabled=_get_bool("MEMORY_DISTILL_ENABLED", True),
        memory_nudge_cooldown_hours=_get_int("MEMORY_NUDGE_COOLDOWN_HOURS", 24),
        memory_governance_enabled=_get_bool("MEMORY_GOVERNANCE_ENABLED", True),
        ingest_token=_get("INGEST_TOKEN"),
        console_token=_get("CONSOLE_TOKEN"),
        gmail_scopes=tuple(scopes),
        db_path=_get("DB_PATH", "./data/assistant.db"),
        log_path=_get("LOG_PATH", "./data/assistant.log"),
        prompts_dir=_get("PROMPTS_DIR", "./prompts"),
        timezone=_get("TIMEZONE", "America/Los_Angeles"),
        morning_brief_hour=_get_int("MORNING_BRIEF_HOUR", 8),
        evening_brief_hour=_get_int("EVENING_BRIEF_HOUR", 18),
        whatsapp_enabled=_get_bool("WHATSAPP_ENABLED", False),
        whatsapp_relay_port=_get_int("WHATSAPP_RELAY_PORT", 7999),
        whatsapp_send_port=_get_int("WHATSAPP_SEND_PORT", 7998),
        personal_jids=_get_csv("PERSONAL_JIDS", lower=True),
        vip_jids=_get_csv("VIP_JIDS", lower=True),
        mute_jids=_get_csv("MUTE_JIDS", lower=True),
        watch_keywords=_get_csv("WATCH_KEYWORDS", lower=True),
        wa_user_jid=_get("WA_USER_JID").lower(),
        whatsapp_transcribe_model=_get("WHATSAPP_TRANSCRIBE_MODEL", "google/gemini-2.5-flash"),
        whatsapp_settle_enabled=_get_bool("WHATSAPP_SETTLE_ENABLED", True),
        whatsapp_settle_seconds=_get_int("WHATSAPP_SETTLE_SECONDS", 75),
        whatsapp_settle_max_hold_seconds=_get_int("WHATSAPP_SETTLE_MAX_HOLD_SECONDS", 900),
        whatsapp_group_settle_seconds=_get_int("WHATSAPP_GROUP_SETTLE_SECONDS", 300),
        whatsapp_group_max_hold_seconds=_get_int("WHATSAPP_GROUP_MAX_HOLD_SECONDS", 10800),
        presence_suppression_enabled=_get_bool("PRESENCE_SUPPRESSION_ENABLED", True),
        presence_outbound_cooldown_seconds=_get_int("PRESENCE_OUTBOUND_COOLDOWN_SECONDS", 300),
        presence_app_focus_enabled=_get_bool("PRESENCE_APP_FOCUS_ENABLED", True),
        read_receipt_quiet_hours_enabled=_get_bool("READ_RECEIPT_QUIET_HOURS_ENABLED", True),
        read_receipt_quiet_start_hour=_get_int("READ_RECEIPT_QUIET_START_HOUR", 22),
        read_receipt_quiet_end_hour=_get_int("READ_RECEIPT_QUIET_END_HOUR", 8),
        whatsapp_context_days=_get_int("WHATSAPP_CONTEXT_DAYS", 14),
        whatsapp_silence_sweep_enabled=_get_bool("WHATSAPP_SILENCE_SWEEP_ENABLED", True),
        wa_stuck_secs=_get_int("WA_STUCK_SECS", 120),
        wa_unanswered_out_secs=_get_int("WA_UNANSWERED_OUT_SECS", 21600),
        wa_unanswered_in_secs=_get_int("WA_UNANSWERED_IN_SECS", 10800),
        wa_silence_sweep_interval_secs=_get_int("WA_SILENCE_SWEEP_INTERVAL_SECS", 1800),
        wa_silence_max_age_secs=_get_int("WA_SILENCE_MAX_AGE_SECS", 345600),
        whatsapp_style_enabled=_get_bool("WHATSAPP_STYLE_ENABLED", True),
        feedback_tuning_enabled=_get_bool("FEEDBACK_TUNING_ENABLED", True),
        feedback_skip_threshold=_get_int("FEEDBACK_SKIP_THRESHOLD", 3),
        retention_enabled=_get_bool("RETENTION_ENABLED", True),
        retention_days=_get_int("RETENTION_DAYS", 90),
        retention_wa_history_days=_get_int("RETENTION_WA_HISTORY_DAYS", 30),
        relay_health_alert_enabled=_get_bool("RELAY_HEALTH_ALERT_ENABLED", True),
        relay_stale_alert_seconds=_get_int("RELAY_STALE_ALERT_SECONDS", 180),
        relay_status_path=_get("RELAY_STATUS_PATH", "relay/status.json"),
        stuck_send_minutes=_get_int("STUCK_SEND_MINUTES", 30),
        miniapp_url=_get("MINIAPP_URL"),
        miniapp_secret=_get("MINIAPP_SECRET"),
        opportunity_detection_enabled=_get_bool("OPPORTUNITY_DETECTION_ENABLED", True),
        project_tagging_enabled=_get_bool("PROJECT_TAGGING_ENABLED", True),
        state_update_hour=_get_int("STATE_UPDATE_HOUR", 7),
    )
    return _cached

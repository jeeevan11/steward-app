"""First-run onboarding: learn the user's world before going live.

On the very first start we do a one-time, best-effort sweep of the user's recent
SENT mail to:
  * mine voice samples (how they actually write) → ``voice_samples``,
  * build a voice profile summary the drafter prepends to its system prompt,
  * infer likely VIPs by tallying who they email most → contact stats, and
  * seed one or two conservative default rules (e.g. file newsletters).

Everything is wrapped so that ANY failure logs and degrades to a partial summary —
onboarding must never crash startup. After a successful (even partial) run we set
the ``onboarded`` kv flag so it does not repeat.
"""

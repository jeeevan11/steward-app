"""The brain: classify a thread, decide its tier, apply hard guardrails.

Pipeline: classifier (haiku noise pass → opus judgment) produces a `Decision`;
`tiers.decide` combines it with contact memory + guardrails into a `FinalDecision`.
The guardrails and tier engine are pure functions (stdlib only) and are the most
heavily tested part of the system.
"""

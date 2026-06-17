# MEMORY.md — what the assistant remembers about your contacts, and why

Plain-English guide to the memory layer. The short version: the assistant tries to
know a person and the situation *before* it does anything — the way you already do
when a name pops up on WhatsApp — so that most things resolve without bothering you.
Knowing the context is what *reduces* the nudging.

---

## The three layers

**Layer 1 — the conversation.** The current email thread or chat. It always had this.

**Layer 2 — the relationship record (one per person).** A small, living note the
assistant keeps about each contact and loads *before* it reads a new message:
- who they are (name, company, role, how they relate to you, segment);
- what's **open** right now (a quote you're waiting on, a reply someone owes);
- what's already been **decided** (commitments you made, things you declined);
- a short log of what the **assistant itself** recently did with them (drafted /
  sent / surfaced / you skipped).

It is **distilled, not hoarded.** After an exchange, a cheap model pass updates the
note with what actually changed — it never pastes raw emails in, and the note is
size-capped so it stays a one-pager, not an archive. (Big dumped context makes the
decisions *worse*, not better.)

**Layer 3 — one identity across channels.** A single "person" can own an email
address *and* a WhatsApp number. So when a supplier emails you a quote and then
WhatsApps "did you see my email?", the assistant knows that's **one person, one
situation** — not two strangers.

---

## How it decides to LINK two identities vs ASK you

A wrong merge silently corrupts two people's memory and is hard to notice later, so
the bar to merge automatically is deliberately high. It links **without asking** only
on a genuinely unambiguous signal:
- an email address written **inside a WhatsApp message** that already belongs to a
  known person;
- a phone number in an **email signature** that matches a known WhatsApp number;
- an exact **full name + the same company domain** matching exactly one person.

Anything weaker (a shared first name, the same name on a free email domain, a hunch)
is **never** merged silently. Instead you get **one** Telegram question:
"🔗 Same person? Is [WhatsApp +91…] the same as [email x@y.com]?  ✅ Yes / ❌ No."
- **Yes** links them permanently.
- **No** is remembered — it will never ask about that pair again.

Everything keeps working if you ignore it; they just stay separate.

---

## Recency and conflicts — the latest message always wins

Memory **informs**; it never overrides what the newest message plainly says.
- If a new message contradicts a remembered fact ("ignore my last email, we went
  with someone else"), the **new message wins** and the old fact is archived.
- If the record is old and the message implies things have moved on, the assistant
  **trusts the message** and re-distills — it won't act on a stale fact.
- If a new message conflicts with memory on something **consequential** (money,
  legal, an investor, a commitment, anything irreversible), the assistant **stops and
  asks you.** A confidently-wrong memory ("I thought you agreed to this") is more
  dangerous than no memory, so this is a hard rule, not a suggestion.

---

## How memory REDUCES nudging (the whole point)

Once the assistant understands a situation, the bar to interrupt you **rises**:
- If it already showed you something and you **skipped** it, it won't re-surface the
  same thing for a cooldown window unless something materially changed.
- If a situation is already **resolved**, it won't re-open it.
- A known, low-stakes thread that's tracking fine is handled at the **lowest safe
  level** instead of pinging you.

This quieting can **only ever lower** a nudge, and never past the safety lines below.

---

## The safety lines memory can never cross

- **Never below a guardrail floor.** Anything money/legal/investor/irreversible is
  floored to "ask" by the deterministic guardrails; memory can quiet ordinary chatter
  but can never drop one of these below that floor.
- **Never a silent action on something that needs you.** A "please decide this" item
  can be quieted to a one-line FYI at most — never to a silent auto-action.
- **Personal/family people are always surfaced.** If you've marked someone personal
  (or they're a personal contact on any channel), they're **never** auto-handled, no
  matter how much the assistant knows about them or how confident it is.
- **If memory breaks, it falls back to today.** Every memory step is best-effort; if
  resolution or loading ever fails, the assistant simply classifies on the thread
  alone — exactly as it did before memory existed. Memory is additive; it can't break
  the inbox.

---

## Where it lives
All on your Mac, in the same local database — `persons`, `person_links`,
`relationship_memory` (the note), and `person_link_suggestions` (the "same person?"
questions). Nothing about your contacts leaves the machine. Switches in `.env`:
`MEMORY_ENABLED`, `MEMORY_DISTILL_ENABLED`, `LINK_SUGGESTIONS_ENABLED`,
`MEMORY_NUDGE_COOLDOWN_HOURS`. Engineering detail lives in
[INTELLIGENCE.md](INTELLIGENCE.md).

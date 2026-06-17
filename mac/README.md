# Steward — macOS menu-bar app

A native SwiftUI menu-bar app (the "top-bar widget") that is the seamless shell over the
Python engine. The owner onboards **once**, then it just runs — launches at login, stays
on 24/7, and supervises everything in the background.

## What it does

- **Live top-bar icon** — the glyph alone when all is quiet; a small count appears only
  when items are waiting for you. No color, no animation, no badge: information, never a
  nag. Dims when the agent is off.
- **Calm popover** — status line (running, mode, pending, WhatsApp), the one **Agent
  on/off** switch, Open dashboard, Settings.
- **Native dashboard** (Apple-style, symmetric) — status hero, four stat tiles (waiting
  for you · handled 24h · WhatsApp · mode), and every control in one place: **Email**,
  **WhatsApp**, and **Open at login** toggles, plus a button to the full web console.
- **Open at login** — registers via `SMAppService` so it's always there after a reboot.
- **One-time onboarding** — collects the OpenRouter key + Telegram details, picks the repo
  folder, and hands off Gmail sign-in and the WhatsApp QR to Terminal.
- **Adopts a running stack** — if the engine/relay/dashboard are already up (e.g. started
  from a terminal), it detects them (heartbeat + ports) and never launches duplicates.

It **supervises three processes** (it owns no logic of its own):
- `\.venv/bin/python run.py` — the engine
- `\.venv/bin/python run_web.py` — the dashboard backend (:8000)
- `node relay/whatsapp_relay.js` — the WhatsApp relay (only when WhatsApp is on)

Status is read from `data/status.json` (engine heartbeat) and `relay/status.json`.

## Build

Requires Xcode command-line tools (Swift 5.9+). macOS 13+.

```bash
cd mac
./build_app.sh          # → "Steward.app"
open "Steward.app"
```

The script compiles the Swift package, wraps it into a proper `.app` bundle
(`LSUIElement` = no Dock icon), and ad-hoc signs it so launch-at-login works locally.

Move it to `/Applications` for the cleanest experience. On first launch the setup window
opens automatically; after that it's menu-bar only.

## First-run setup (what the owner does once)

1. Launch the app → the setup window opens.
2. Confirm the **repo path** (defaults to `~/Desktop/JatinDhull`).
3. Paste the **OpenRouter API key** and **Telegram bot token + chat id**, choose dry-run or
   live, **Save**.
4. **Connect Gmail** → a Terminal opens running `run.py --onboard` (Google sign-in).
5. **Link WhatsApp** → a Terminal opens running the relay; scan the QR in
   WhatsApp ▸ Linked devices ▸ Link a device.
6. **Start the agent.** Turn on **Open at login** and you're done.

## Notes / limitations

- The app supervises the existing repo + virtualenv (it does not yet bundle Python/Node).
  Packaging a fully standalone, notarized `.app` (embedded Python via PyInstaller, Node
  sidecar, Developer ID + notarization) is the remaining distribution step.
- The dashboard at :8000 serves the built React UI when `frontend/dist` exists
  (`cd assistant/web/frontend && npm install && npm run build`). In dev, run Vite
  (`npm run dev`, :5173) and point the dashboard URL there.
- `osascript`/Terminal is used only for the two interactive setup flows (Gmail OAuth,
  WhatsApp QR), so the owner can see the browser/QR.

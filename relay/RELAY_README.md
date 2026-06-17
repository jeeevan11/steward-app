# WhatsApp relay (the dumb pipe)

This Node process is the ONLY thing that talks to WhatsApp. It has no brain — it
forwards incoming messages to the Python assistant and performs send/read commands
the assistant gives it. All triage, drafting, and decisions happen in Python.

## Prerequisites
- Node.js 18+ (for global `fetch`)
- The Python assistant running with `WHATSAPP_ENABLED=true` (it listens on
  `127.0.0.1:7999/inbound`).

## Install & run
```
cd relay
npm install
node whatsapp_relay.js     # or: npm start
```

## Pairing (one time)
On first run it prints a QR code in the terminal. On your phone:
**WhatsApp → Settings → Linked devices → Link a device → scan the QR.**
After it says "WhatsApp connected", you're done. The session is saved to
`relay/session/` so it **won't ask again on restart**.

## The `session/` folder
- It holds your multi-device WhatsApp credentials (encrypted keys Baileys needs).
- **Keep it.** Deleting it logs the relay out and forces a fresh QR pairing.
- It's gitignored — never commit it.

## Restarting
Just stop (Ctrl+C) and run `node whatsapp_relay.js` again. It reconnects using the
saved session. If the connection drops on its own, it auto-reconnects with
exponential backoff (up to 60s between tries).

## If it logs out
If WhatsApp logs the device out (you removed it from Linked devices, or it expired),
the relay prints a message and exits. To recover: delete `relay/session/` and run it
again to re-pair with a fresh QR.

## Ports (must match the Python `.env`)
- `WHATSAPP_RELAY_PORT` (default 7999) — where Python listens; the relay POSTs
  incoming messages there (`/inbound`).
- `WHATSAPP_SEND_PORT` (default 7998) — where the relay listens; Python POSTs
  `/send` and `/read` there.
Set them as env vars if you change the defaults: `WHATSAPP_SEND_PORT=7998 node whatsapp_relay.js`.

## Files it writes
- `relay/relay.log` — every send/receive with a timestamp.
- `relay/status.json` — connection state, session age, messages today, last message
  time. The Telegram `/wastatus` command reads this.

## Safety
- Sends are delayed 2–8 seconds (randomised) so they don't look robotic.
- **Do not install "anti-ban" npm packages** — they are malware. Jitter is the only
  mitigation used here.

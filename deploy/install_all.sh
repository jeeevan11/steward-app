#!/usr/bin/env bash
# Install ALL THREE Steward processes (engine, web console, WhatsApp relay) as macOS
# launchd LaunchAgents so they start at login, run in the background, and auto-restart
# on crash — closing the "nothing survives a reboot / no supervisor" gap.
#
# Usage:
#   ./deploy/install_all.sh            # install & load all three
#   ./deploy/install_all.sh uninstall  # stop & remove all three
#
# Run this on the machine that will actually run Steward, AFTER your .env is set and a
# first `python run.py --onboard` has completed. It replaces manual `python run.py` etc.
set -euo pipefail

WORKDIR="$(cd "$(dirname "$0")/.." && pwd)"
LA="$HOME/Library/LaunchAgents"
UID_N="$(id -u)"

if [[ -x "${WORKDIR}/.venv/bin/python" ]]; then PYTHON="${WORKDIR}/.venv/bin/python"; else PYTHON="$(command -v python3)"; fi
NODE="$(command -v node || true)"; [[ -z "$NODE" && -x /opt/homebrew/bin/node ]] && NODE="/opt/homebrew/bin/node"
[[ -z "$NODE" && -x /usr/local/bin/node ]] && NODE="/usr/local/bin/node"

declare -a LABELS=("com.cos.assistant" "com.cos.web" "com.cos.relay")

uninstall() {
  for L in "${LABELS[@]}"; do
    launchctl bootout "gui/${UID_N}/${L}" 2>/dev/null || launchctl unload "$LA/${L}.plist" 2>/dev/null || true
    rm -f "$LA/${L}.plist"
    echo "removed ${L}"
  done
  exit 0
}
[[ "${1:-}" == "uninstall" ]] && uninstall

echo "Installing Steward launchd agents:"
echo "  workdir: ${WORKDIR}"
echo "  python:  ${PYTHON}"
echo "  node:    ${NODE:-<not found — relay will not start>}"
mkdir -p "${WORKDIR}/data" "$LA"

fill() {  # <template> <dest>
  sed -e "s|{{PYTHON}}|${PYTHON}|g" -e "s|{{NODE}}|${NODE}|g" -e "s|{{WORKDIR}}|${WORKDIR}|g" "$1" > "$2"
}

fill "${WORKDIR}/deploy/com.cos.assistant.plist.template" "$LA/com.cos.assistant.plist"
fill "${WORKDIR}/deploy/com.cos.web.plist.template"       "$LA/com.cos.web.plist"
[[ -n "$NODE" ]] && fill "${WORKDIR}/deploy/com.cos.relay.plist.template" "$LA/com.cos.relay.plist"

for L in "${LABELS[@]}"; do
  [[ -f "$LA/${L}.plist" ]] || continue
  launchctl bootout "gui/${UID_N}/${L}" 2>/dev/null || true
  launchctl bootstrap "gui/${UID_N}" "$LA/${L}.plist"
  launchctl enable "gui/${UID_N}/${L}"
  echo "loaded ${L}"
done

cat <<EOF

Done. All three run at login + restart on crash. Useful:
  launchctl print gui/${UID_N}/com.cos.assistant   # engine status
  launchctl kickstart -k gui/${UID_N}/com.cos.web   # restart web now
  tail -f ${WORKDIR}/data/assistant.log
  ./deploy/install_all.sh uninstall                 # remove all three

NOTE: stop any manually-started processes first (the engine's single-instance lock will
otherwise refuse the launchd copy). The relay may print a QR on first launch — scan once.
EOF

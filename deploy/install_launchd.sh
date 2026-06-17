#!/usr/bin/env bash
# Install the chief-of-staff assistant as a macOS launchd LaunchAgent so it runs
# in the background, starts at login, and restarts if it crashes.
#
# Usage:
#   ./deploy/install_launchd.sh            # install & load
#   ./deploy/install_launchd.sh uninstall  # stop & remove
set -euo pipefail

LABEL="com.cos.assistant"
WORKDIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"
TEMPLATE="${WORKDIR}/deploy/${LABEL}.plist.template"

# Prefer the project venv's python if present, else whatever python3 is on PATH.
if [[ -x "${WORKDIR}/.venv/bin/python" ]]; then
  PYTHON="${WORKDIR}/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

uninstall() {
  echo "Stopping and removing ${LABEL}…"
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || launchctl unload "${PLIST_DEST}" 2>/dev/null || true
  rm -f "${PLIST_DEST}"
  echo "Removed ${PLIST_DEST}"
}

if [[ "${1:-}" == "uninstall" ]]; then
  uninstall
  exit 0
fi

echo "Installing ${LABEL}"
echo "  workdir: ${WORKDIR}"
echo "  python:  ${PYTHON}"

mkdir -p "${WORKDIR}/data" "$HOME/Library/LaunchAgents"

sed -e "s|{{PYTHON}}|${PYTHON}|g" \
    -e "s|{{WORKDIR}}|${WORKDIR}|g" \
    "${TEMPLATE}" > "${PLIST_DEST}"

# Reload cleanly (bootout first in case it's already loaded).
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "${PLIST_DEST}"
launchctl enable "gui/$(id -u)/${LABEL}"

echo
echo "Installed and loaded. Useful commands:"
echo "  launchctl print gui/$(id -u)/${LABEL}      # status"
echo "  launchctl kickstart -k gui/$(id -u)/${LABEL}  # restart now"
echo "  tail -f ${WORKDIR}/data/assistant.log      # app logs"
echo "  ./deploy/install_launchd.sh uninstall      # remove"

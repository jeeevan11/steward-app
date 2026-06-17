#!/usr/bin/env bash
# Build the menu-bar executable and wrap it into a proper .app bundle (no Dock icon).
# Usage:  ./build_app.sh   ->  produces "Chief of Staff.app" in this folder.
# ASCII-only on purpose (avoids locale/multibyte parsing surprises across shells).
set -eo pipefail
cd "$(dirname "$0")"

APP="Steward.app"
BIN=".build/release/CosBar"
rm -rf "Chief of Staff.app"   # remove the old name if present

echo "[1/3] Compiling..."
swift build -c release

echo "[2/3] Assembling ${APP}..."
rm -rf "${APP}"
mkdir -p "${APP}/Contents/MacOS" "${APP}/Contents/Resources"
cp "${BIN}" "${APP}/Contents/MacOS/CosBar"
chmod +x "${APP}/Contents/MacOS/CosBar"

# App icon — regenerate Steward.icns from AppIcon.png if present, then bundle it.
if [ -f "AppIcon.png" ]; then
  rm -rf Steward.iconset && mkdir Steward.iconset
  for spec in "16:16x16" "32:16x16@2x" "32:32x32" "64:32x32@2x" "128:128x128" \
              "256:128x128@2x" "256:256x256" "512:256x256@2x" "512:512x512" "1024:512x512@2x"; do
    px="${spec%%:*}"; name="${spec##*:}"
    sips -z "$px" "$px" AppIcon.png --out "Steward.iconset/icon_${name}.png" >/dev/null
  done
  iconutil -c icns Steward.iconset -o Steward.icns && rm -rf Steward.iconset
fi
[ -f "Steward.icns" ] && cp "Steward.icns" "${APP}/Contents/Resources/Steward.icns"

cat > "${APP}/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Steward</string>
  <key>CFBundleDisplayName</key><string>Steward</string>
  <key>CFBundleIconFile</key><string>Steward</string>
  <key>CFBundleIdentifier</key><string>com.local.steward</string>
  <key>CFBundleExecutable</key><string>CosBar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>CFBundleURLTypes</key>
  <array><dict>
    <key>CFBundleURLName</key><string>com.local.steward</string>
    <key>CFBundleURLSchemes</key><array><string>steward</string></array>
  </dict></array>
</dict>
</plist>
PLIST

echo "APPL????" > "${APP}/Contents/PkgInfo"

# Ad-hoc sign so launch-at-login (SMAppService) works locally. For distribution,
# replace "-" with a Developer ID and notarize (see README.md).
echo "[3/3] Ad-hoc signing..."
codesign --force --deep --sign - "${APP}" 2>/dev/null || echo "  (codesign skipped)"

echo "Built: ${PWD}/${APP}"
echo "Run it:  open \"${APP}\"   (or move it to /Applications)"

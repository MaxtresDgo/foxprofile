#!/usr/bin/env bash
# Builds dist/FoxProfileAgent-<version>-macos.pkg on macOS. Needs: uv, and the stock
# macOS tools pkgbuild, productbuild, sips, iconutil.
#
# The pkg installs "/Applications/FoxProfile Agent.app" (a relocatable CPython from
# python-build-standalone, the macOS wheels of requirements.txt and the app code) and
# a LaunchAgent that starts it at every login. Every user's data stays in
# ~/Library/Application Support/FoxProfile (`src.agent --home`).
#
# Apple silicon and macOS 15+ only (Distribution.xml refuses anything else): fpgen,
# which Camoufox needs for fingerprints, imports indexed_zstd, whose only macOS wheel
# that works is macosx_15_0_arm64 (older ones miss libzstd; Intel has none).
# ponytail: unsigned; the operator opens the pkg with right-click > Open once. Sign
# with a Developer ID (productsign) and notarize to drop that step.
set -euo pipefail

# HUB_URL=https://hub.example.com (required) bakes the hub the agent connects to
# (dev or prod build); FOXPROFILE_HUB_URL in the user's .env or --hub still win.
HUB_URL=${HUB_URL:-}
case "$HUB_URL" in
http://?* | https://?*) ;;
*) echo "HUB_URL must be the hub's http(s) URL, e.g. HUB_URL=https://hub.example.com $0" >&2; exit 1 ;;
esac

PY_RELEASE=20260924
PY_VERSION=3.12.14
PY_SHA256=c2edb321cd32ec2b170df208db0446dccc4398db602ca27cf2079098fb1f7d9d
LABEL=io.github.chienbm98.foxprofile-agent
# uv picks wheels for this macOS (it defaults to an older one, and so to broken wheels).
export MACOSX_DEPLOYMENT_TARGET=15.0

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/pyproject.toml")
build=$root/build/agent-pkg
stage=$build/stage
app="$stage/root/Applications/FoxProfile Agent.app"
res=$app/Contents/Resources
rm -rf "$stage" && mkdir -p "$app/Contents/MacOS" "$res/app" "$stage/root/Library/LaunchAgents" "$root/dist"

tarball=cpython-$PY_VERSION+$PY_RELEASE-aarch64-apple-darwin-install_only_stripped.tar.gz
[ -f "$build/$tarball" ] || curl -fsSL -o "$build/$tarball" \
    "https://github.com/astral-sh/python-build-standalone/releases/download/$PY_RELEASE/$tarball"
echo "$PY_SHA256  $build/$tarball" | shasum -a 256 -c -
tar -xzf "$build/$tarball" -C "$res" # -> Resources/python

site=$res/python/lib/python3.12/site-packages
uv pip install -q --target "$site" \
    --python-platform aarch64-apple-darwin --python-version 3.12 --only-binary :all: \
    -r "$root/requirements.txt"
# Console-script launchers made for this host (their shebangs carry the build paths)
# and pyobjc's own test suite.
rm -rf "$site/bin" "$site/PyObjCTest"
# The app is read-only for operators: seed fpgen's model like the Windows MSI does
# (dated 2100 so fpgen never refetches it).
camoufox_version=$(basename "$site"/camoufox-*.dist-info .dist-info | cut -d- -f2)
uv run -q --no-project -p 3.12 --with "camoufox==$camoufox_version" python -c \
    'import sys, pathlib; from camoufox.fpgen_model import ensure_fpgen_model as f; f(pathlib.Path(sys.argv[1]))' \
    "$site/fpgen/data"

cp -R "$root/src" "$root/chrome_engine" "$res/app/"
rm -rf "$res/app/chrome_engine/tests" "$res/app/chrome_engine/e2e"
# Updates from the hub (src/hub/agent_code.py): BUILD names this code, the
# requirements say which hub code still fits these libraries, and the hook makes
# this Python load a newer code the agent unpacked for this very BUILD.
cp "$root/requirements.txt" "$res/app/"
printf '%s\n' "${HUB_URL%/}" >"$res/app/HUB_URL"
PYTHONPATH="$root" uv run -q --no-project -p 3.12 python -c \
    'import sys, pathlib; from src.hub.agent_code import code_id; print(code_id(pathlib.Path(sys.argv[1])))' \
    "$res/app" >"$res/app/BUILD"
cp "$root/installer/agent_update_hook.py" "$site/_foxprofile_update.py"
echo "import _foxprofile_update" >"$site/foxprofile_update.pth"
# The Chrome engine's browser (fingerprint-chromium, BSD-3, pinned by SHA-256), so
# Chrome profiles open on the first launch instead of downloading it then. Cached
# under build/ between builds; chrome_engine/fetch.py finds it in builds/.
chrome_version=$(PYTHONPATH="$root" uv run -q --no-project -p 3.12 python -c \
    'import sys, pathlib; from chrome_engine import fetch, release
fetch.install(home=pathlib.Path(sys.argv[1]), host=sys.argv[2]); print(release.DEFAULT_VERSION)' \
    "$build/chrome-cache" macos)
mkdir -p "$res/app/chrome_engine/builds"
cp -R "$build/chrome-cache/$chrome_version" "$res/app/chrome_engine/builds/"
cp "$root/chrome_engine/LICENSE.fingerprint-chromium.txt" "$res/app/chrome_engine/builds/"
find "$stage" -name __pycache__ -type d -prune -exec rm -rf {} +
# Read-only for operators, so Python could never cache bytecode there. -s/-p keep
# this machine's paths out of the .pyc files.
# ponytail: a few third-party files fail to compile (py2 leftovers); never imported.
uv run -q --no-project -p 3.12 python -m compileall -q -j0 \
    -s "$stage/root" -p / "$res/app" "$site" >/dev/null || true

iconset=$stage/icon.iconset
mkdir "$iconset"
for size in 16 32 128 256 512; do
    sips -z $size $size "$root/src/assets/icon.png" --out "$iconset/icon_${size}x${size}.png" >/dev/null
    if [ $size -le 256 ]; then
        sips -z $((size * 2)) $((size * 2)) "$root/src/assets/icon.png" \
            --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
    fi
done
iconutil -c icns "$iconset" -o "$res/icon.icns"
install -m 755 "$here/foxprofile-agent" "$app/Contents/MacOS/foxprofile-agent"
sed "s/__VERSION__/$version/" "$here/Info.plist" >"$app/Contents/Info.plist"
cp "$here/$LABEL.plist" "$stage/root/Library/LaunchAgents/"

# Drop quarantine (Gatekeeper) and other attributes. com.apple.provenance stays on
# local builds (macOS protects it) and ships as ._ entries; it is harmless, and CI
# builds do not have it.
xattr -cr "$stage/root"
# Installer would otherwise "relocate" a bundle (ours, or the Chromium.app inside it)
# onto any copy with the same bundle id elsewhere on the Mac.
pkgbuild --analyze --root "$stage/root" "$stage/components.plist" >/dev/null
uv run -q --no-project -p 3.12 python -c 'import plistlib, sys
with open(sys.argv[1], "rb") as f: bundles = plistlib.load(f)
for b in bundles: b["BundleIsRelocatable"] = False
with open(sys.argv[1], "wb") as f: plistlib.dump(bundles, f)' "$stage/components.plist"
chmod 755 "$here/scripts/preinstall" "$here/scripts/postinstall"
pkgbuild --root "$stage/root" --component-plist "$stage/components.plist" \
    --identifier "$LABEL" --version "$version" --install-location / \
    --scripts "$here/scripts" "$stage/agent.pkg" >/dev/null
sed "s/__VERSION__/$version/" "$here/Distribution.xml" >"$stage/Distribution.xml"
productbuild --distribution "$stage/Distribution.xml" --package-path "$stage" \
    "$root/dist/FoxProfileAgent-$version-macos.pkg" >/dev/null
echo "dist/FoxProfileAgent-$version-macos.pkg"

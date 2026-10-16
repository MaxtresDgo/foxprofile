#!/usr/bin/env bash
# Builds dist/FoxProfileAgent-<version>.msi from Linux or macOS.
# Needs: uv (fetches the Windows wheels, evaluating markers for Windows, and a
# Python 3.12 to byte-compile), msitools (wixl, wixl-heat), unzip, curl.
#
# The MSI carries the embeddable Python from python.org, the Windows wheels of
# requirements.txt and the app code, all under Program Files\FoxProfile Agent.
# Every Windows user's data stays in %LOCALAPPDATA%\FoxProfile (`src.agent --home`).
set -euo pipefail

PY_VERSION=3.12.10
PY_SHA256=4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3
PY_TAG=python312

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/pyproject.toml")
build=$root/build/agent-msi
stage=$build/stage
rm -rf "$stage" && mkdir -p "$stage/python" "$stage/app" "$root/dist"

zip=$build/python-$PY_VERSION-embed-amd64.zip
[ -f "$zip" ] || curl -fsSL -o "$zip" \
    "https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-embed-amd64.zip"
echo "$PY_SHA256  $zip" | shasum -a 256 -c -
unzip -q "$zip" -d "$stage/python"
# The ._pth file replaces sys.path in the embeddable Python (PYTHONPATH is ignored).
printf '%s\r\n' "$PY_TAG.zip" . '..\app' 'Lib\site-packages' 'import site' \
    > "$stage/python/$PY_TAG._pth"

uv pip install -q --target "$stage/python/Lib/site-packages" \
    --python-platform x86_64-pc-windows-msvc --python-version 3.12 --only-binary :all: \
    -r "$root/requirements.txt"
# Console-script launchers made for this host: useless on Windows, and their
# shebangs carry the build machine's paths.
rm -rf "$stage/python/Lib/site-packages/bin"
# Camoufox installs fpgen's model inside the fpgen package, which operators cannot
# write under Program Files: seed it here with the same camoufox release. Its files
# are dated 2100 so fpgen never tries to refresh them (it refetches after 5 weeks).
site=$stage/python/Lib/site-packages
camoufox_version=$(basename "$site"/camoufox-*.dist-info .dist-info | cut -d- -f2)
uv run -q --no-project -p 3.12 --with "camoufox==$camoufox_version" python -c \
    'import sys, pathlib; from camoufox.fpgen_model import ensure_fpgen_model as f; f(pathlib.Path(sys.argv[1]))' \
    "$site/fpgen/data"

cp -R "$root/src" "$root/chrome_engine" "$stage/app/"
rm -rf "$stage/app/chrome_engine/tests" "$stage/app/chrome_engine/e2e"
find "$stage" -name __pycache__ -type d -prune -exec rm -rf {} +
# Program Files is read-only for operators, so Python could never cache bytecode there.
# ponytail: a few third-party files fail to compile (py2 leftovers); they are never imported.
uv run -q --no-project -p 3.12 python -m compileall -q -j0 "$stage/app" "$stage/python/Lib/site-packages" >/dev/null || true

(cd "$stage" && find . -type f | sed 's|^\./||' | sort) |
    wixl-heat --prefix "" --var var.Stage --component-group Payload \
        --directory-ref INSTALLDIR --win64 > "$build/payload.wxs"
wixl -a x64 -D Win64=yes -D Version="$version" -D Stage="$stage" \
    -o "$root/dist/FoxProfileAgent-$version.msi" "$here/foxprofile-agent.wxs" "$build/payload.wxs"
echo "dist/FoxProfileAgent-$version.msi"

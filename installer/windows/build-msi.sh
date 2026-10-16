#!/usr/bin/env bash
# Builds dist/FoxProfileAgent-<version>.msi from Linux or macOS.
# Needs: uv (fetches the Windows wheels, evaluating markers for Windows, and a
# Python 3.12 to byte-compile), msitools (wixl, wixl-heat, msiextract), unzip, curl.
#
# The MSI carries the embeddable Python from python.org, the Windows wheels of
# requirements.txt and the app code, all under Program Files\FoxProfile Agent.
# Every Windows user's data stays in %LOCALAPPDATA%\FoxProfile (`src.agent --home`).
set -euo pipefail

PY_VERSION=3.12.10
PY_SHA256=4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3
PY_TAG=python312
# Tcl/Tk for the agent's log window (src/agent_logs.py): the embeddable Python has none.
TCLTK_SHA256=55c96ffad69b1c834aa52e11b9ce41637a178ba6ad6607e83956044834276e2a

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
tcltk=$build/tcltk-$PY_VERSION-amd64.msi
[ -f "$tcltk" ] || curl -fsSL -o "$tcltk" \
    "https://www.python.org/ftp/python/$PY_VERSION/amd64/tcltk.msi"
echo "$TCLTK_SHA256  $tcltk" | shasum -a 256 -c -
rm -rf "$build/tcltk" && msiextract -C "$build/tcltk" "$tcltk" >/dev/null
# _tkinter.pyd and its DLLs next to python.exe; Tcl finds its library in <prefix>\tcl.
cp "$build"/tcltk/DLLs/*.pyd "$build"/tcltk/DLLs/*.dll "$stage/python/"
cp -R "$build/tcltk/tcl" "$stage/python/"
rm -rf "$stage/python/tcl/nmake"

uv pip install -q --target "$stage/python/Lib/site-packages" \
    --python-platform x86_64-pc-windows-msvc --python-version 3.12 --only-binary :all: \
    -r "$root/requirements.txt"
# Console-script launchers made for this host: useless on Windows, and their
# shebangs carry the build machine's paths.
rm -rf "$stage/python/Lib/site-packages/bin"
cp -R "$build/tcltk/Lib/tkinter" "$stage/python/Lib/site-packages/"
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
# Updates from the hub (src/hub/agent_code.py): BUILD names this code, the
# requirements say which hub code still fits these libraries, and the hook makes
# this Python load a newer code the agent unpacked for this very BUILD.
cp "$root/requirements.txt" "$stage/app/"
PYTHONPATH="$root" uv run -q --no-project -p 3.12 python -c \
    'import sys, pathlib; from src.hub.agent_code import code_id; print(code_id(pathlib.Path(sys.argv[1])))' \
    "$stage/app" >"$stage/app/BUILD"
cp "$root/installer/agent_update_hook.py" "$site/_foxprofile_update.py"
echo "import _foxprofile_update" >"$site/foxprofile_update.pth"
# The Chrome engine's browser (fingerprint-chromium, BSD-3, pinned by SHA-256), so
# Chrome profiles open on the first launch instead of downloading it then. Cached
# under build/ between builds; chrome_engine/fetch.py finds it in builds/.
chrome_version=$(PYTHONPATH="$root" uv run -q --no-project -p 3.12 python -c \
    'import sys, pathlib; from chrome_engine import fetch, release
fetch.install(home=pathlib.Path(sys.argv[1]), host=sys.argv[2]); print(release.DEFAULT_VERSION)' \
    "$build/chrome-cache" windows-x64)
mkdir -p "$stage/app/chrome_engine/builds"
cp -R "$build/chrome-cache/$chrome_version" "$stage/app/chrome_engine/builds/"
cp "$root/chrome_engine/LICENSE.fingerprint-chromium.txt" "$stage/app/chrome_engine/builds/"
find "$stage" -name __pycache__ -type d -prune -exec rm -rf {} +
# Program Files is read-only for operators, so Python could never cache bytecode there.
# ponytail: a few third-party files fail to compile (py2 leftovers); they are never imported.
uv run -q --no-project -p 3.12 python -m compileall -q -j0 "$stage/app" "$stage/python/Lib/site-packages" >/dev/null || true

(cd "$stage" && find . -type f | sed 's|^\./||' | sort) |
    wixl-heat --prefix "" --var var.Stage --component-group Payload \
        --directory-ref INSTALLDIR --win64 > "$build/payload.wxs"
# The id wixl-heat gave pythonw.exe, for the StartAgent custom action.
pythonw=$(sed -n 's|.*File Id="\([^"]*\)".*Source="$(var.Stage)/python/pythonw.exe".*|\1|p' "$build/payload.wxs")
: "${pythonw:?pythonw.exe is missing from the payload}"
wixl -a x64 -D Win64=yes -D Version="$version" -D Stage="$stage" -D PythonW="$pythonw" \
    -o "$root/dist/FoxProfileAgent-$version.msi" "$here/foxprofile-agent.wxs" "$build/payload.wxs"
echo "dist/FoxProfileAgent-$version.msi"

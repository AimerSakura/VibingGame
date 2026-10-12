#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python_exe="${VIBE_BUILD_PYTHON:-python3}"
library_root="$repo_root/.build/pythonlibs"

if [[ ! -d "$library_root/PyInstaller" ]]; then
	echo "PyInstaller is missing from .build/pythonlibs. Install build dependencies first." >&2
	exit 1
fi

export PYTHONPATH="$library_root:$repo_root"
dist="$repo_root/.build/sidecar-dist"
work="$repo_root/.build/sidecar-work"
spec="$repo_root/.build/sidecar-spec"
mkdir -p "$dist" "$work" "$spec"

help_text="$("$python_exe" -m PyInstaller --help 2>&1)"
if grep -qE -- '--target-arch([ =]|$)' <<<"$help_text"; then
	arch_flag=(--target-arch arm64)
else
	arch_flag=(--target-architecture arm64)
fi

"$python_exe" -m PyInstaller \
	--noconfirm --clean --onedir --console \
	--name godotvibe-agent \
	--distpath "$dist" --workpath "$work" --specpath "$spec" \
	--paths "$repo_root" --paths "$library_root" \
	"${arch_flag[@]}" \
	--add-data "$repo_root/vibe_tools/prompts:vibe_tools/prompts" \
	--add-data "$repo_root/vibe_tools/api_cache:vibe_tools/api_cache" \
	--collect-submodules vibe_tools.templates \
	--collect-all openai --collect-all uvicorn \
	--exclude-module pandas --exclude-module openpyxl \
	--exclude-module lxml --exclude-module numpy \
	--hidden-import uvicorn.logging \
	--hidden-import uvicorn.loops.auto \
	--hidden-import uvicorn.protocols.http.auto \
	--hidden-import uvicorn.protocols.websockets.auto \
	--hidden-import uvicorn.lifespan.on \
	"$repo_root/vibe_tools/editor_sidecar.py"

sidecar="$dist/godotvibe-agent/godotvibe-agent"
if [[ ! -x "$sidecar" ]]; then
	echo "Sidecar executable was not created: $sidecar" >&2
	exit 1
fi
echo "Sidecar created: $sidecar"

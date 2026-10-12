#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
output="$repo_root/.build/VibeGodot-macOS-arm64"
app="$output/VibeGodot.app"
engine="$repo_root/bin/godot.macos.editor.arm64"
sidecar="$repo_root/.build/sidecar-dist/godotvibe-agent"
template="$repo_root/misc/dist/macos_tools.app"
archive="$repo_root/.build/VibeGodot-macOS-arm64.zip"

if [[ ! -f "$engine" ]]; then
	echo "Editor binary not found: $engine" >&2
	exit 1
fi
if [[ ! -x "$sidecar/godotvibe-agent" ]]; then
	echo "Agent sidecar not found. Run misc/vibe_agent/build_sidecar.sh first." >&2
	exit 1
fi
case "$output" in
	"$repo_root"/.build/*) ;;
	*) echo "For safety, output must be under $repo_root/.build" >&2; exit 1 ;;
esac

rm -rf "$output"
mkdir -p "$output"
cp -R "$template" "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources/vibe_agent"
cp "$engine" "$app/Contents/MacOS/VibeGodot"
chmod +x "$app/Contents/MacOS/VibeGodot"
rm -rf "$app/Contents/Resources/vibe_agent/godotvibe-agent"
cp -R "$sidecar" "$app/Contents/Resources/vibe_agent/godotvibe-agent"

/usr/libexec/PlistBuddy -c "Set :CFBundleExecutable VibeGodot" "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleName VibeGodot" "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.vibegodot.editor" "$app/Contents/Info.plist"

cp "$repo_root/LICENSE.txt" "$output/GODOT_LICENSE.txt"
cp "$repo_root/misc/vibe_agent/README.zh-CN.md" "$output/使用说明.md"
cp "$repo_root/misc/vibe_agent/CAPABILITIES.md" "$output/能力对应表.md"

rm -f "$archive"
ditto -c -k --sequesterRsrc --keepParent "$app" "$archive"
echo "Package created: $archive"

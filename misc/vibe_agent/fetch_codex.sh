#!/usr/bin/env bash
# Fetch, verify and stage the official Codex CLI release for macOS arm64.
#
# This is the single source of truth for the Codex version and release layout
# used by misc/vibe_agent/package_macos.sh. Only macOS on Apple silicon is
# supported: the script downloads the official aarch64-apple-darwin package
# from releases.openai.com (with an official GitHub Releases fallback),
# verifies the SHA-256 manifest before extracting, and validates the extracted
# layout, Mach-O architecture and ``codex --version`` output.
#
# Usage:
#   fetch_codex.sh
#       Fetch (or reuse) the pinned Codex release and print the absolute path
#       of the prepared release directory on stdout. Diagnostics go to stderr.
#   fetch_codex.sh --validate DIR [VERSION]
#       Verify an extracted release layout and print the reported Codex
#       version. VERSION defaults to the version recorded in
#       codex-package.json.
#
# Environment overrides (verification is never weakened):
#   CODEX_VERSION       Release version to fetch (default: 0.146.0).
#   CODEX_RELEASE_ROOT  Reuse this already-extracted release layout instead of
#                       downloading it.
#   CODEX_BIN           Reuse the release layout that contains this codex
#                       executable instead of downloading it.
#
# Downloads and build output stay under the repository .build directory.
# Archives, credentials and logs containing secrets are never committed.

set -euo pipefail

default_codex_version="0.146.0"
codex_target="aarch64-apple-darwin"
codex_asset="codex-package-${codex_target}.tar.gz"
codex_checksum_asset="codex-package_SHA256SUMS"
releases_base_url="https://releases.openai.com/codex"
github_base_url="https://github.com/openai/codex/releases/download"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
build_root="$repo_root/.build/codex"

log() { printf 'fetch_codex: %s\n' "$*" >&2; }
die() { printf 'fetch_codex: error: %s\n' "$*" >&2; exit 1; }
have_command() { command -v "$1" >/dev/null 2>&1; }

usage() {
	cat <<'EOF'
Usage:
  fetch_codex.sh
      Fetch or reuse the pinned Codex macOS arm64 release and print its path.
  fetch_codex.sh --validate DIR [VERSION]
      Verify an extracted release layout and print its Codex version.

Environment:
  CODEX_VERSION       Release version to fetch (default: 0.146.0).
  CODEX_RELEASE_ROOT  Reuse this extracted release layout instead of downloading.
  CODEX_BIN           Reuse the release layout containing this codex executable.
EOF
}

validate_version() {
	[[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.]+)?$ ]] ||
		die "invalid CODEX_VERSION: $1"
}

require_macos_arm64() {
	[[ "$(uname -s)" == "Darwin" ]] || die "Codex packaging is only supported on macOS"
	local arch
	arch="$(uname -m)"
	if [[ "$arch" == "x86_64" ]] &&
		[[ "$(sysctl -n sysctl.proc_translated 2>/dev/null || true)" == "1" ]]; then
		arch="arm64"
	fi
	case "$arch" in
		arm64 | aarch64) ;;
		*) die "Codex packaging requires Apple silicon (arm64); found $arch" ;;
	esac
}

version_from_binary() {
	local binary="$1"
	"$binary" --version 2>/dev/null | tr -d '\r' |
		awk 'NF { token = $NF } END { if (token != "") print token }' || true
}

sha256_file() {
	local file="$1"
	if have_command shasum; then
		LC_ALL=C shasum -a 256 "$file" | awk '{print $1}'
	elif have_command sha256sum; then
		LC_ALL=C sha256sum "$file" | awk '{print $1}'
	elif have_command openssl; then
		LC_ALL=C openssl dgst -sha256 "$file" | sed 's/^.*= //'
	else
		die "shasum, sha256sum or openssl is required to verify the download"
	fi
}

download_file() {
	local url="$1" output="$2"
	if have_command curl; then
		curl -fsSL --connect-timeout 10 --max-time 300 "$url" -o "$output"
	elif have_command wget; then
		wget -q -t 1 -T 300 -O "$output" "$url"
	else
		die "curl or wget is required to download Codex"
	fi
}

download_with_fallback() {
	local primary="$1" fallback="$2" output="$3"
	if download_file "$primary" "$output"; then
		return 0
	fi
	log "download failed: $primary"
	log "retrying from GitHub Releases"
	download_file "$fallback" "$output"
}

manifest_digest() {
	local manifest="$1" asset="$2"
	awk -v asset="$asset" '
		length($1) == 64 && $1 !~ /[^0-9a-fA-F]/ && $2 == asset {
			print tolower($1)
			found = 1
			exit
		}
		END { if (!found) exit 1 }
	' "$manifest"
}

read_manifest() {
	local manifest="$1"
	have_command python3 || return 0
	python3 - "$manifest" <<'PY' || true
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
except Exception:
    raise SystemExit(0)
if isinstance(data, dict):
    print("{}\t{}".format(data.get("version", ""), data.get("target", "")))
PY
}

make_executable() {
	local file="$1"
	if [[ -f "$file" ]]; then
		chmod 0755 "$file"
	fi
	return 0
}

# Verify an extracted release layout. Prints the reported version on stdout and
# a reason for failure on stderr. Returns non-zero on any mismatch.
check_layout() {
	local layout="$1" expected="$2"
	local binary="$layout/bin/codex"

	have_command file || {
		printf 'the file command is required to inspect the Codex binary\n' >&2
		return 1
	}
	[[ -d "$layout" ]] || {
		printf 'release directory not found: %s\n' "$layout" >&2
		return 1
	}
	[[ -f "$binary" ]] || {
		printf 'missing bin/codex in %s\n' "$layout" >&2
		return 1
	}
	[[ -x "$binary" ]] || {
		printf 'bin/codex is not executable in %s\n' "$layout" >&2
		return 1
	}
	local entry
	for entry in codex-resources codex-path; do
		[[ -d "$layout/$entry" ]] || {
			printf 'missing %s in %s\n' "$entry" "$layout" >&2
			return 1
		}
	done
	[[ -f "$layout/codex-package.json" ]] || {
		printf 'missing codex-package.json in %s\n' "$layout" >&2
		return 1
	}

	local description
	description="$(file -b "$binary" 2>/dev/null || true)"
	case "$description" in
		*Mach-O*arm64*) ;;
		*)
			printf 'bin/codex is not an arm64 Mach-O: %s\n' "$description" >&2
			return 1
			;;
	esac

	local reported
	reported="$(version_from_binary "$binary")"
	[[ -n "$reported" ]] || {
		printf 'cannot read codex --version from %s\n' "$binary" >&2
		return 1
	}

	local manifest_line manifest_version manifest_target
	manifest_line="$(read_manifest "$layout/codex-package.json")"
	manifest_version="${manifest_line%%$'\t'*}"
	manifest_target="${manifest_line##*$'\t'}"
	if [[ -n "$manifest_version" && "$manifest_version" != "$reported" ]]; then
		printf 'codex-package.json version %s does not match codex --version %s\n' \
			"$manifest_version" "$reported" >&2
		return 1
	fi
	if [[ -n "$manifest_target" && "$manifest_target" != "$codex_target" ]]; then
		printf 'codex-package.json target %s is not %s\n' "$manifest_target" "$codex_target" >&2
		return 1
	fi
	if [[ -n "$expected" && "$reported" != "$expected" ]]; then
		printf 'codex --version %s does not match requested %s\n' "$reported" "$expected" >&2
		return 1
	fi
	if [[ -n "$manifest_version" && -n "$expected" && "$manifest_version" != "$expected" ]]; then
		printf 'codex-package.json version %s does not match requested %s\n' \
			"$manifest_version" "$expected" >&2
		return 1
	fi

	printf '%s' "$reported"
}

mode="fetch"
validate_dir=""
validate_version=""
case "${1:-}" in
	"")
		mode="fetch"
		;;
	--validate)
		mode="validate"
		validate_dir="${2:-}"
		validate_version="${3:-}"
		[[ -n "$validate_dir" ]] || die "--validate requires a release directory"
		[[ $# -le 3 ]] || die "unexpected extra arguments after --validate"
		;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		die "unknown argument: $1"
		;;
esac

if [[ "$mode" == "validate" ]]; then
	resolved_version="$validate_version"
	if [[ -z "$resolved_version" ]]; then
		manifest_line="$(read_manifest "$validate_dir/codex-package.json")"
		resolved_version="${manifest_line%%$'\t'*}"
	fi
	if ! reported="$(check_layout "$validate_dir" "$resolved_version")"; then
		die "invalid Codex release at $validate_dir"
	fi
	printf '%s\n' "$reported"
	exit 0
fi

require_macos_arm64

codex_version="${CODEX_VERSION:-$default_codex_version}"
validate_version "$codex_version"
explicit_version="false"
if [[ -n "${CODEX_VERSION:-}" ]]; then
	explicit_version="true"
fi

if [[ -n "${CODEX_RELEASE_ROOT:-}" || -n "${CODEX_BIN:-}" ]]; then
	override_root=""
	if [[ -n "${CODEX_RELEASE_ROOT:-}" ]]; then
		override_root="$CODEX_RELEASE_ROOT"
	else
		[[ -f "$CODEX_BIN" ]] || die "CODEX_BIN is not a file: $CODEX_BIN"
		bin_dir="$(cd "$(dirname "$CODEX_BIN")" && pwd)"
		if [[ "$(basename "$bin_dir")" == "bin" ]]; then
			override_root="$(dirname "$bin_dir")"
		else
			override_root="$bin_dir"
		fi
	fi
	[[ -d "$override_root" ]] || die "release root not found: $override_root"

	expected="$codex_version"
	if [[ "$explicit_version" != "true" ]]; then
		manifest_line="$(read_manifest "$override_root/codex-package.json")"
		expected="${manifest_line%%$'\t'*}"
	fi
	if ! reported="$(check_layout "$override_root" "$expected")"; then
		die "invalid Codex release at $override_root"
	fi
	override_root="$(cd "$override_root" && pwd)"
	log "using verified Codex $reported from $override_root"
	printf '%s\n' "$override_root"
	exit 0
fi

mkdir -p "$build_root"
release_dir="$build_root/$codex_version-$codex_target"

if [[ -d "$release_dir" ]]; then
	if reported="$(check_layout "$release_dir" "$codex_version" 2>/dev/null)"; then
		log "reusing verified Codex $reported at $release_dir"
		printf '%s\n' "$release_dir"
		exit 0
	fi
	log "cached release at $release_dir failed verification; refetching"
	rm -rf "$release_dir"
fi

tmp_dir="$(mktemp -d "$build_root/.download.XXXXXX")"
stage=""
trap '[[ -n "${tmp_dir:-}" ]] && rm -rf "$tmp_dir"; [[ -n "${stage:-}" ]] && rm -rf "$stage"' EXIT

manifest="$tmp_dir/$codex_checksum_asset"
archive="$tmp_dir/$codex_asset"
manifest_url="$releases_base_url/releases/$codex_version/$codex_checksum_asset"
archive_url="$releases_base_url/releases/$codex_version/$codex_asset"
manifest_fallback="$github_base_url/rust-v$codex_version/$codex_checksum_asset"
archive_fallback="$github_base_url/rust-v$codex_version/$codex_asset"

log "downloading $codex_checksum_asset for Codex $codex_version"
download_with_fallback "$manifest_url" "$manifest_fallback" "$manifest" ||
	die "failed to download $codex_checksum_asset for Codex $codex_version"
expected_digest="$(manifest_digest "$manifest" "$codex_asset" || true)"
[[ -n "$expected_digest" ]] ||
	die "no SHA-256 entry for $codex_asset in $codex_checksum_asset"

log "downloading $codex_asset for Codex $codex_version"
download_with_fallback "$archive_url" "$archive_fallback" "$archive" ||
	die "failed to download $codex_asset for Codex $codex_version"
actual_digest="$(sha256_file "$archive")"
[[ "$actual_digest" == "$expected_digest" ]] ||
	die "SHA-256 mismatch for $codex_asset (expected $expected_digest, got $actual_digest)"
log "SHA-256 verified for $codex_asset"

stage="$build_root/.staging.$codex_version.$$"
rm -rf "$stage"
mkdir -p "$stage"
tar -xzf "$archive" -C "$stage"
make_executable "$stage/bin/codex"
make_executable "$stage/bin/codex-code-mode-host"
make_executable "$stage/codex-path/rg"
make_executable "$stage/codex-resources/bwrap"
# Match the official standalone installer layout: expose bin/codex at the root.
ln -sf "bin/codex" "$stage/codex"

if ! reported="$(check_layout "$stage" "$codex_version")"; then
	die "downloaded Codex $codex_version failed layout verification"
fi

rm -rf "$release_dir"
mv "$stage" "$release_dir"
stage=""
log "staged verified Codex $reported at $release_dir"
printf '%s\n' "$release_dir"

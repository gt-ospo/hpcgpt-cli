#!/usr/bin/env bash
# Sets up opencode + the sandboxed Slurm MCP server for this PACE deployment.
# Run this script on the Phoenix login node.
#
# `module load python` first -- without it, the system default (Python 3.9) on
# PATH is too old for this server.
#
# This script compiles/installs socat and rg (ripgrep) to ~/.local/bin, which
# is used by the sandboxing code, on the first run. In this case, you should run
# this script on a compute node.
set -euo pipefail

usage() {
    cat <<EOF
Usage: $(basename "$0") <slurm-account>

Installs opencode (if missing), sets up the Slurm MCP server's venv and a
real <account>.slurm_caps.json charging the given account, and prints the
env vars to export before running opencode.

Example:
    $(basename "$0") gts-myproject
EOF
}

if [[ $# -ne 1 || "$1" == "-h" || "$1" == "--help" ]]; then
    usage
    exit "$([[ $# -eq 1 && ( "$1" == "-h" || "$1" == "--help" ) ]] && echo 0 || echo 1)"
fi

SLURM_ACCOUNT="$1"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACE_DIR="$(dirname "$SCRIPT_DIR")"
SLURM_SERVER_DIR="$PACE_DIR/mcp_servers/slurm_server"
HPCGPT_ROOT="$(dirname "$PACE_DIR")"

echo "== Installing opencode =="
if command -v opencode >/dev/null 2>&1; then
    echo "opencode already installed: $(command -v opencode)"
else
    curl -fsSL https://opencode.ai/install | bash
    export PATH="$HOME/.opencode/bin:$PATH"
fi

echo "== Setting up the Slurm MCP server =="
cd "$SLURM_SERVER_DIR"

if [[ ! -d .venv ]]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

CAPS_FILE="$SLURM_ACCOUNT.slurm_caps.json"

if [[ -f "$CAPS_FILE" ]]; then
    echo "$CAPS_FILE already exists, leaving it as-is"
else
    python3 - "$SLURM_ACCOUNT" example.slurm_caps.json "$CAPS_FILE" <<'PY'
import json
import sys

account, src, dst = sys.argv[1:4]
with open(src) as f:
    caps = json.load(f)
caps["account"] = account
with open(dst, "w") as f:
    json.dump(caps, f, indent=2)
    f.write("\n")
PY
    echo "Wrote $CAPS_FILE, charging account: $SLURM_ACCOUNT"
fi

echo "== Checking opencode-sandbox's runtime deps (ripgrep, socat) =="
VENDOR_BIN="$HOME/.local/bin"
mkdir -p "$VENDOR_BIN"
export PATH="$VENDOR_BIN:$PATH"
PATH_EXPORT_LINE="export PATH=$VENDOR_BIN:\$PATH"

if ! command -v rg >/dev/null 2>&1; then
    module load ripgrep 2>/dev/null || true
fi
if command -v rg >/dev/null 2>&1; then
    echo "rg found: $(command -v rg)"
else
    echo "rg not found -- fetching a static binary into $VENDOR_BIN"
    RG_VERSION=15.2.0
    RG_TARBALL="ripgrep-$RG_VERSION-x86_64-unknown-linux-musl.tar.gz"
    # Pinned against the .sha256 file GitHub publishes alongside the release
    # (not re-derived from a search result) -- this one's automatable since
    # it's independently verifiable; socat below isn't, for the same reason.
    RG_SHA256="33e15bcf1624b25cdd2a55813a47a2f95dbe126268203e76aa6a585d1e7b149c"
    RG_TMP="$(mktemp -d)"
    curl -fsSL -o "$RG_TMP/$RG_TARBALL" \
        "https://github.com/BurntSushi/ripgrep/releases/download/$RG_VERSION/$RG_TARBALL"
    echo "$RG_SHA256  $RG_TMP/$RG_TARBALL" | sha256sum -c -
    tar -xzf "$RG_TMP/$RG_TARBALL" -C "$RG_TMP"
    cp "$RG_TMP/ripgrep-$RG_VERSION-x86_64-unknown-linux-musl/rg" "$VENDOR_BIN/rg"
    chmod +x "$VENDOR_BIN/rg"
    rm -rf "$RG_TMP"
    echo "Installed rg $RG_VERSION to $VENDOR_BIN/rg"
fi

if ! command -v socat >/dev/null 2>&1; then
    module load socat 2>/dev/null || true
fi
if command -v socat >/dev/null 2>&1; then
    echo "socat found: $(command -v socat)"
else
    if ! command -v gcc >/dev/null 2>&1 || ! command -v make >/dev/null 2>&1; then
        module load gcc 2>/dev/null || true
    fi
    if ! command -v gcc >/dev/null 2>&1 || ! command -v make >/dev/null 2>&1; then
        echo "ERROR: socat not found and no gcc/make available to build it" \
            "(tried 'module load gcc') -- can't continue." >&2
        exit 1
    fi

    echo "socat not found -- building from source into $VENDOR_BIN"
    SOCAT_VERSION=1.8.1.3
    # dest-unreach.org doesn't publish a checksum or signature for this
    # tarball (checked: nothing on the download page, no .sig/.asc
    # alongside it) -- fetched straight from the canonical upstream domain
    # itself, which is the most that's available to verify against here.
    SOCAT_TMP="$(mktemp -d)"
    curl -fsSL -o "$SOCAT_TMP/socat.tar.gz" \
        "http://www.dest-unreach.org/socat/download/socat-$SOCAT_VERSION.tar.gz"
    tar -xzf "$SOCAT_TMP/socat.tar.gz" -C "$SOCAT_TMP"
    (
        cd "$SOCAT_TMP/socat-$SOCAT_VERSION"
        # Falls back to disabling optional libs per the README's own
        # troubleshooting guidance if a full-featured build fails (we don't
        # know what -devel packages, if any, are on this login node).
        ./configure || ./configure --disable-openssl --disable-readline --disable-libwrap
        make
    )
    cp "$SOCAT_TMP/socat-$SOCAT_VERSION/socat" "$VENDOR_BIN/socat"
    chmod +x "$VENDOR_BIN/socat"
    rm -rf "$SOCAT_TMP"
    echo "Built socat $SOCAT_VERSION to $VENDOR_BIN/socat"
fi

# opencode-sandbox's allowWrite replaces its defaults rather than extending
# them, so the project dir (".") and /tmp are listed explicitly alongside
# $HOME/scratch, not just the latter on its own.
#
# "mode": "enforce" because the plugin's own default ("permissive") just
# runs commands unsandboxed if the sandbox fails to initialize (e.g. bwrap
# missing/broken) -- silently, with no indication anything was skipped.
# Enforce makes that failure loud (blocked bash calls) instead of a
# silent no-op that looks identical to a working sandbox.
SANDBOX_CONFIG=$(printf '{"mode":"enforce","filesystem":{"allowWrite":[".", "/tmp", "%s/scratch"]}}' "$HOME")

cat <<EOF

== Done ==

Export these, then run opencode:

export HPCGPT_ROOT=$HPCGPT_ROOT
export OPENCODE_CONFIG=$SCRIPT_DIR/opencode.jsonc
export PACE_SLURM_CAPS=$SLURM_SERVER_DIR/$CAPS_FILE
export OPENCODE_SANDBOX_CONFIG='$SANDBOX_CONFIG'
$PATH_EXPORT_LINE
opencode

NOTE: Using opencode's default free model for now.

NOTE: Adjust the embers/inferno walltime budgets in:
$SLURM_SERVER_DIR/$CAPS_FILE
EOF

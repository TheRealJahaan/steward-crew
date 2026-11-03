#!/usr/bin/env bash
# Set up Steward Crew on this Mac, next to Switch Console.
#
#   ./scripts/setup.sh            install, store the GitHub token, register the Ledger
#   ./scripts/setup.sh --token    replace the stored GitHub token
#
# Safe to run again: each step skips itself when it is already done.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="${STEWARD_HOME:-$HOME/.steward}"
VENV="$HOME_DIR/venv"
BIN="$HOME_DIR/bin"
KEYCHAIN_SERVICE="steward-ledger-github"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }

mkdir -p "$HOME_DIR" "$BIN"
chmod 700 "$HOME_DIR"

# 1. Python and the steward commands ─────────────────────────────────────────
say "1/5  Installing the steward commands"
if ! command -v uv >/dev/null 2>&1; then
  echo "  Steward needs uv (a Python installer). Install it with:"
  echo "      brew install uv"
  echo "  or: curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "  then run this script again."
  exit 1
fi
if [ ! -x "$VENV/bin/python" ]; then
  uv venv --quiet --python 3.12 "$VENV"
fi
uv pip install --quiet --python "$VENV/bin/python" -e "$REPO_DIR"
ln -sf "$VENV/bin/steward" "$BIN/steward"
ln -sf "$VENV/bin/steward-ledger" "$BIN/steward-ledger"
ok "installed $BIN/steward and $BIN/steward-ledger"

if [ ! -f "$HOME_DIR/policy.yaml" ]; then
  cp "$REPO_DIR/policy.yaml" "$HOME_DIR/policy.yaml"
  ok "copied policy.yaml to $HOME_DIR/policy.yaml (edit that copy)"
else
  ok "kept your $HOME_DIR/policy.yaml"
fi

# 2. The GitHub token, for the Ledger only ───────────────────────────────────
say "2/5  GitHub token for the Ledger"
have_token() { security find-generic-password -s "$KEYCHAIN_SERVICE" >/dev/null 2>&1; }
if [ "${1:-}" = "--token" ] || ! have_token; then
  echo "  Paste the fine-grained token for the demo repository (Issues and"
  echo "  Pull requests: read and write; Contents: read). It goes into your"
  echo "  macOS Keychain, not into any file. macOS asks for it twice; nothing"
  echo "  shows while you type or paste."
  security add-generic-password -a steward -s "$KEYCHAIN_SERVICE" -U -w
  ok "stored in the Keychain as '$KEYCHAIN_SERVICE'"
else
  ok "already in the Keychain (run with --token to replace it)"
fi

# 3. Register the Ledger with Switch ─────────────────────────────────────────
say "3/5  Registering the Ledger with Switch"
if [ -f "$HOME_DIR/ledger.json" ]; then
  ok "already registered (delete $HOME_DIR/ledger.json to register again)"
else
  "$BIN/steward-ledger" setup || {
    warn "Registration failed. Is Switch Console open with the local server running?"
    exit 1
  }
fi

# 4. The project's code, for the agents to read ──────────────────────────────
say "4/5  Fetching the project's code for the agents"
"$BIN/steward-ledger" code

# 5. First read of GitHub ────────────────────────────────────────────────────
say "5/5  Reading GitHub"
"$BIN/steward-ledger" sync
"$BIN/steward" status

say "Done. Next:"
cat <<EOF
  • Slack #maintainers:   /invite @Agent Switch   then   !invite-agent @ledger
  • Start the Ledger:     $BIN/steward-ledger run
  • Switch Console → Templates → New template → paste
    switch/steward-crew.template.yaml → Use
  • Demo data (never sent to GitHub):  $BIN/steward demo flood
EOF

#!/usr/bin/env bash
# Put bibkey on PATH. Safe to re-run; it only creates a symlink.
set -euo pipefail
cd "$(dirname "$0")"

chmod +x bibkey.py
mkdir -p "$HOME/.local/bin"
ln -sfn "$(pwd)/bibkey.py" "$HOME/.local/bin/bibkey"

echo "linked $HOME/.local/bin/bibkey -> $(readlink -f "$HOME/.local/bin/bibkey")"
"$HOME/.local/bin/bibkey" --version

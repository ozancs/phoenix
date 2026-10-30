#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
KLIPPER_DIR="${KLIPPER_DIR:-$HOME/klipper}"
for module in phoenix.py phoenix_core.py; do
    target="$KLIPPER_DIR/klippy/extras/$module"
    if [[ -L "$target" && "$(readlink -- "$target")" == "$ROOT/klippy/extras/$module" ]]; then
        rm -- "$target"
    fi
done
echo 'Phoenix module links removed. Config and recovery records were preserved.'
echo 'Remove the phoenix.cfg include and slicer commands before restarting Klipper.'

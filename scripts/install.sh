#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
KLIPPER_DIR="${KLIPPER_DIR:-$HOME/klipper}"
CONFIG_DIR="${CONFIG_DIR:-$HOME/printer_data/config}"

if [[ ! -d "$KLIPPER_DIR/klippy/extras" ]]; then
    echo "Klipper not found at $KLIPPER_DIR. Set KLIPPER_DIR and retry." >&2
    exit 1
fi
mkdir -p -- "$CONFIG_DIR"
for module in phoenix.py phoenix_core.py; do
    target="$KLIPPER_DIR/klippy/extras/$module"
    source="$ROOT/klippy/extras/$module"
    if [[ -e "$target" || -L "$target" ]]; then
        if [[ -L "$target" && "$(readlink -- "$target")" == "$source" ]]; then
            continue
        fi
        echo "Refusing to overwrite $target. Review the existing installation." >&2
        exit 1
    fi
done
for module in phoenix.py phoenix_core.py; do
    ln -sfn -- "$ROOT/klippy/extras/$module" "$KLIPPER_DIR/klippy/extras/$module"
done
if [[ ! -e "$CONFIG_DIR/phoenix.cfg" ]]; then
    cp -- "$ROOT/config/phoenix.cfg" "$CONFIG_DIR/phoenix.cfg"
fi
cat <<'MESSAGE'
Phoenix installed. No service was restarted and printer.cfg was not changed.
1. Review phoenix.cfg; merge [force_move] if it already exists.
2. Add [include phoenix.cfg] to printer.cfg.
3. Review park/path settings before enabling profile_ready.
4. Add PHOENIX_START / PHOENIX_END to your slicer or print macros.
5. Restart Klipper when the printer is idle, then run PHOENIX_STATUS.
Read docs/TESTING.md before the first recovery test.
MESSAGE

#!/usr/bin/env bash
# Установка без вопросов (для автоматизации и проверок); обычно удобнее мастер setup.sh.
#   sudo bash install.sh base [--binaries download|dir|build|skip] [--binaries-dir DIR] [--web-config FILE]
#   sudo bash install.sh happ --subscription FILE --instances FILE
#   sudo bash install.sh upgrade [--binaries keep|download|dir|build] [--binaries-dir DIR]
#   sudo bash install.sh cleanup-legacy
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
command -v python3 >/dev/null || { echo 'Нужен python3: sudo apt install python3' >&2; exit 1; }
exec python3 "$HERE/lib/installer.py" "$@"

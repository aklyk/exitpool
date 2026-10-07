#!/usr/bin/env bash
# Мастер exitpool: sudo bash setup.sh [--plan | --add | --upgrade] [--binaries-dir DIR | --build-binaries] [--force-budget]
# Прокси для скачиваний лучше передавать переменной: sudo EXITPOOL_PROXY=socks5h://127.0.0.1:1080 bash setup.sh
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
command -v python3 >/dev/null || { echo 'Нужен python3: sudo apt install python3' >&2; exit 1; }
exec python3 "$HERE/lib/setup.py" "$@"

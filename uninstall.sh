#!/usr/bin/env bash
# Удаление exitpool.
#   sudo bash uninstall.sh               остановить и отключить выходы и веб (всё остальное остаётся)
#   sudo bash uninstall.sh --purge       убрать программу, службы и сетевые объекты; ключи и настройки остаются
#   sudo bash uninstall.sh --purge --all то же и удалить ключи, профили AWG, подписку и настройки
# Исходящие в 3x-ui не трогаются: уберите их из балансеров сами.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
LIB="$HERE/lib"
[[ -f "$LIB/installer.py" ]] || LIB=/opt/exitpool/lib
case "${1:-}" in
  '') exec python3 "$LIB/installer.py" uninstall ;;
  --purge)
    if [[ "${2:-}" == --all ]]; then
      read -r -p 'Удалить также ключи, профили AWG, подписку и настройки без возможности восстановления? (да/нет) ' answer </dev/tty
      [[ "$answer" == да ]] || { echo 'Отменено.'; exit 0; }
      # The purge removes /opt/exitpool, so run a copy of the installer from a temporary place.
      TMP=$(mktemp -d); cp -r "$LIB" "$TMP/lib"; [[ -d "$LIB/../release" ]] && cp -r "$LIB/../release" "$TMP/release"
      python3 "$TMP/lib/installer.py" purge --all && rc=0 || rc=$?; rm -rf "$TMP"; exit $rc
    fi
    TMP=$(mktemp -d); cp -r "$LIB" "$TMP/lib"; [[ -d "$LIB/../release" ]] && cp -r "$LIB/../release" "$TMP/release"
    python3 "$TMP/lib/installer.py" purge && rc=0 || rc=$?; rm -rf "$TMP"; exit $rc ;;
  *) echo 'Использование: sudo bash uninstall.sh [--purge [--all]]' >&2; exit 2 ;;
esac

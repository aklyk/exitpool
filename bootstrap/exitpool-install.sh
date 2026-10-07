#!/usr/bin/env bash
# exitpool — загрузчик из gist. Скачивает архив релиза с GitHub, сверяет SHA-256 и запускает мастер.
#
#   sudo bash -c 'bash <(curl -fsSL https://gist.githubusercontent.com/aklyk/5d7c8f9eb430163320eb5fbce4b0fac8/raw/exitpool-install.sh)'
#   sudo bash -c 'bash <(curl -fsSL …/exitpool-install.sh) --upgrade'      (под root: bash <(curl -fsSL …))
#   `sudo bash <(curl …)` не работает: sudo закрывает /dev/fd/63, через который bash читает скрипт.
#
# Опции загрузчика:
#   --version X.Y.Z   другая версия (хеш берётся из SHA256SUMS релиза, а не из этого файла)
#   --from FILE       архив exitpool-X.Y.Z.tar.gz, принесённый вручную (если GitHub недоступен с сервера)
#   --proxy URL       прокси для скачиваний: http://, https://, socks5h://, socks5://, socks4a://, socks4://,
#                     можно с логином: http://user:pass@host:3128. Пароль в --proxy виден в ps, пока идёт
#                     установка; безопаснее передать его переменной EXITPOOL_PROXY или ввести в мастере
# Остальные опции передаются мастеру: --plan, --add, --upgrade, --binaries-dir DIR, --build-binaries, --force-budget.
set -euo pipefail

VERSION=@VERSION@
SHA256=@SHA256@
REPO=aklyk/exitpool

die() { echo "exitpool: $*" >&2; exit 1; }
[[ $(id -u) -eq 0 ]] || die "нужны права root: sudo bash -c 'bash <(curl -fsSL …/exitpool-install.sh)'"
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || die 'нужен Linux amd64'
for tool in curl tar sha256sum python3; do
  command -v "$tool" >/dev/null || die "нет $tool (sudo apt install $tool)"
done

FROM='' PINNED=1 ARGS=()
PROXY=${EXITPOOL_PROXY:-}
while (($#)); do
  case "$1" in
    --version) [[ ${2:-} =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die '--version X.Y.Z'
               [[ $2 == "$VERSION" ]] || PINNED=0; VERSION=$2; shift 2 ;;
    --from) FROM=${2:?--from FILE}; shift 2 ;;
    --proxy) PROXY=${2:?--proxy URL}; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

if [[ $PROXY =~ ^[a-z0-9]+://[^/@]*:[^/@]*@ && -z ${EXITPOOL_PROXY:-} ]]; then
  echo 'Внимание: пароль прокси в аргументе --proxy виден другим процессам (ps). Безопаснее передать его' >&2
  echo 'переменной: sudo EXITPOOL_PROXY=… bash, или ввести прокси в мастере (пароль — скрытым вводом).' >&2
fi

WORK=$(mktemp -d /var/tmp/exitpool-install.XXXXXXXX)
trap 'rm -rf -- "$WORK"' EXIT
ARCHIVE="$WORK/exitpool-$VERSION.tar.gz"

fetch() {   # URL FILE — proxy and credentials only through a private curl config
  local config="$WORK/curl.conf"
  ( umask 077; : > "$config" )
  if [[ -n $PROXY ]]; then
    printf 'proxy = "%s"\n' "${PROXY//\"/\\\"}" >> "$config"
  fi
  curl -fsSL --retry 2 --connect-timeout 20 -K "$config" -o "$2" "$1"
}

if [[ -n $FROM ]]; then
  cp -- "$FROM" "$ARCHIVE"
  if ((PINNED == 0)); then
    SUMS="$(dirname -- "$FROM")/SHA256SUMS"
    [[ -f $SUMS ]] || die "для --version $VERSION с --from положите рядом с архивом SHA256SUMS этого релиза"
    SHA256=$(awk -v f="exitpool-$VERSION.tar.gz" '$2 == f || $2 == "*"f {print $1}' "$SUMS")
  fi
else
  BASE="https://github.com/$REPO/releases/download/v$VERSION"
  echo "Скачиваю exitpool $VERSION с github.com${PROXY:+ (через прокси)}…"
  fetch "$BASE/exitpool-$VERSION.tar.gz" "$ARCHIVE" || die "архив не скачался. Можно указать --proxy URL или принести архив и запустить с --from FILE"
  if ((PINNED == 0)); then
    fetch "$BASE/SHA256SUMS" "$WORK/SHA256SUMS" || die 'SHA256SUMS не скачался'
    SHA256=$(awk -v f="exitpool-$VERSION.tar.gz" '$2 == f || $2 == "*"f {print $1}' "$WORK/SHA256SUMS")
    echo "Хеш взят из SHA256SUMS релиза v$VERSION на GitHub (загрузчик знает только хеш $VERSION по умолчанию)."
  fi
fi
if ((PINNED == 1)); then
  echo "$SHA256  $ARCHIVE" | sha256sum -c --quiet - || die 'SHA-256 архива не совпадает — установка остановлена'
elif [[ -n ${SHA256:-} ]]; then
  echo "$SHA256  $ARCHIVE" | sha256sum -c --quiet - || die 'SHA-256 архива не совпадает с SHA256SUMS'
else
  die 'нет хеша для проверки архива'
fi

# Only files under exitpool-VERSION/, no absolute paths or ..
tar -tzf "$ARCHIVE" > "$WORK/list" || die 'архив повреждён'
if grep -qvE "^exitpool-$VERSION(/|$)" "$WORK/list" || grep -q '\.\./' "$WORK/list"; then
  die 'неожиданная структура архива'
fi
tar -xzf "$ARCHIVE" -C "$WORK" --no-same-owner
DIR="$WORK/exitpool-$VERSION"

export EXITPOOL_PROXY="$PROXY"
# Redirect only the wizard: changing bash's stdin while it reads a pipe
# makes bash wait for more shell source on the terminal.
if [[ -t 0 ]]; then
  python3 "$DIR/lib/setup.py" "${ARGS[@]}"
else
  python3 "$DIR/lib/setup.py" "${ARGS[@]}" </dev/tty
fi

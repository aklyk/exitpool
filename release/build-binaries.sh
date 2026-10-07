#!/usr/bin/env bash
# Build the three AWG binaries for Ubuntu 24.04 amd64 from pinned upstream inputs.
#   release/build-binaries.sh OUTDIR
# Used by CI for releases and by exitpool as a fallback when downloads are not possible.
# Needs network access to github.com, go.dev and Ubuntu mirrors (HTTP(S)_PROXY is honoured).
set -euo pipefail
OUT=${1:?output directory}
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=versions.env
source "$HERE/versions.env"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/exitpool-build.XXXXXXXX")
ADDED=()
record_added() {
  mapfile -t ADDED < <(comm -13 "$WORK/packages.before" <(
    dpkg-query -W -f='${binary:Package} ${db:Status-Status}\n' |
      awk '$2 == "installed" { print $1 }' | LC_ALL=C sort))
}
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ ${#ADDED[@]} -gt 0 && "${EXITPOOL_KEEP_BUILD_TOOLS:-0}" != 1 ]]; then
    echo "Removing packages added for this build: ${ADDED[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq "${ADDED[@]}" ||
      echo 'WARNING: cleanup failed; remove the listed build packages manually.' >&2
  fi
  rm -rf -- "$WORK"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$OUT"

PKGS=(gcc make libc6-dev linux-libc-dev binutils musl-tools git curl ca-certificates unzip)
MISSING=()
for pkg in "${PKGS[@]}"; do
  dpkg-query -W -f='${db:Status-Status}' "$pkg" 2>/dev/null | grep -qx installed || MISSING+=("$pkg")
done
if ((${#MISSING[@]})); then
  echo "Installing build packages: ${MISSING[*]}"
  apt-get update -qq
  dpkg-query -W -f='${binary:Package} ${db:Status-Status}\n' |
    awk '$2 == "installed" { print $1 }' | LC_ALL=C sort > "$WORK/packages.before"
  if ! DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${MISSING[@]}"; then
    record_added
    exit 1
  fi
  record_added
fi

cd "$WORK"
echo "Go $GO_VERSION"
curl -fsSL --retry 3 -o go.tgz "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz"
echo "$GO_SHA256  go.tgz" | sha256sum -c -
tar -xzf go.tgz
export PATH="$WORK/go/bin:$PATH" GOTOOLCHAIN=local CGO_ENABLED=0 GOMAXPROCS=1 \
       GOPATH="$WORK/gopath" GOCACHE="$WORK/gocache" GOFLAGS=-trimpath

git clone -q --depth 1 --branch "$AWG_GO_TAG" "$AWG_GO_REPO" amneziawg-go
test "$(git -C amneziawg-go rev-parse HEAD)" = "$AWG_GO_COMMIT"
(cd amneziawg-go && go mod download && go mod verify && go build -p 1 -ldflags '-s -w' -o "$OUT/amneziawg-go" .)

git clone -q --depth 1 --branch "$AWG_TOOLS_TAG" "$AWG_TOOLS_REPO" amneziawg-tools
test "$(git -C amneziawg-tools rev-parse HEAD)" = "$AWG_TOOLS_COMMIT"
# musl static build: only Linux UAPI headers, never glibc headers, in the include path.
mkdir -p kernel-headers
ln -s /usr/include/linux kernel-headers/linux
ln -s /usr/include/x86_64-linux-gnu/asm kernel-headers/asm
ln -s /usr/include/asm-generic kernel-headers/asm-generic
make -s -C amneziawg-tools/src -j1 CC=musl-gcc LDFLAGS=-static CPPFLAGS="-I$WORK/kernel-headers"
install -m 755 amneziawg-tools/src/wg "$OUT/awg"
strip "$OUT/awg"

curl -fsSL --retry 3 -o xray.zip "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-linux-64.zip"
echo "$XRAY_ZIP_SHA256  xray.zip" | sha256sum -c -
unzip -q xray.zip xray -d "$OUT"
chmod 755 "$OUT/xray"

(cd "$OUT" && sha256sum amneziawg-go awg xray > SHA256SUMS && cat SHA256SUMS)
for bin in amneziawg-go awg xray; do file "$OUT/$bin" | grep -q 'statically linked' || echo "WARNING: $bin is not static"; done


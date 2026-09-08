#!/usr/bin/env bash
# Install the same release binaries locally and in Actions; verify release checksums.
set -euo pipefail
dest="${1:?usage: install-tools.sh ABSOLUTE_BIN_DIRECTORY}"
mkdir -p "$dest"
os=$(uname -s | tr '[:upper:]' '[:lower:]')
case "$(uname -m)" in
  x86_64) arch=amd64 ;;
  arm64|aarch64) arch=arm64 ;;
  *) echo 'Unsupported architecture' >&2; exit 1 ;;
esac
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
download() {
  local base="$1" archive="$2" checksums="$3" binary="$4"
  curl --fail --silent --show-error --location --retry 3 "$base/$archive" -o "$archive"
  curl --fail --silent --show-error --location --retry 3 "$base/$checksums" -o "$checksums"
  awk -v file="$archive" '$2 == file {print}' "$checksums" > selected-checksum
  test -s selected-checksum
  shasum -a 256 -c selected-checksum
  tar -xzf "$archive" "$binary"
  install -m 0755 "$binary" "$dest/$binary"
}
download 'https://github.com/yannh/kubeconform/releases/download/v0.8.0' \
  "kubeconform-$os-$arch.tar.gz" CHECKSUMS kubeconform
download 'https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv5.8.1' \
  "kustomize_v5.8.1_${os}_${arch}.tar.gz" checksums.txt kustomize
download 'https://github.com/rhysd/actionlint/releases/download/v1.7.12' \
  "actionlint_1.7.12_${os}_${arch}.tar.gz" actionlint_1.7.12_checksums.txt actionlint

#!/bin/sh
set -eu

case "${1:-}" in
  -h | --help)
    echo "Install the latest platform-images executable for Linux or macOS."
    echo "Environment: PLATFORM_IMAGES_VERSION, PLATFORM_IMAGES_INSTALL_DIR"
    exit 0
    ;;
esac

repository="davehewy/platform-images"
install_directory="${PLATFORM_IMAGES_INSTALL_DIR:-${HOME}/.local/bin}"
version="${PLATFORM_IMAGES_VERSION:-latest}"

case "$(uname -s)" in
  Linux) operating_system="linux" ;;
  Darwin) operating_system="darwin" ;;
  *)
    echo "platform-images: unsupported operating system: $(uname -s)" >&2
    exit 1
    ;;
esac

case "$(uname -m)" in
  x86_64 | amd64) architecture="amd64" ;;
  arm64 | aarch64) architecture="arm64" ;;
  *)
    echo "platform-images: unsupported architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

if [ -n "${PLATFORM_IMAGES_RELEASE_URL:-}" ]; then
  if [ "$version" = "latest" ]; then
    echo "platform-images: PLATFORM_IMAGES_VERSION is required with PLATFORM_IMAGES_RELEASE_URL" >&2
    exit 1
  fi
  release_version="${version#v}"
  release_url="${PLATFORM_IMAGES_RELEASE_URL%/}"
elif [ "$version" = "latest" ]; then
  latest_release_url="$(
    curl -fsSL -o /dev/null -w '%{url_effective}' \
      "https://github.com/${repository}/releases/latest"
  )"
  latest_release_url="${latest_release_url%/}"
  tag="${latest_release_url##*/}"
  case "$tag" in
    v*) release_version="${tag#v}" ;;
    *)
      echo "platform-images: unable to resolve the latest release version" >&2
      exit 1
      ;;
  esac
  release_url="https://github.com/${repository}/releases/download/${tag}"
else
  release_version="${version#v}"
  tag="v${release_version}"
  release_url="https://github.com/${repository}/releases/download/${tag}"
fi
case "$release_version" in
  "" | *[!0-9A-Za-z.-]*)
    echo "platform-images: invalid release version: ${release_version}" >&2
    exit 1
    ;;
esac

asset="platform-images-v${release_version}-${operating_system}-${architecture}.tar.gz"
legacy_asset="platform-images-${operating_system}-${architecture}.tar.gz"

temporary_directory="$(mktemp -d "${TMPDIR:-/tmp}/platform-images-install.XXXXXX")"
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM

if ! curl -fsSL "${release_url}/${asset}" -o "${temporary_directory}/${asset}" 2>/dev/null; then
  asset="$legacy_asset"
  curl -fsSL "${release_url}/${asset}" -o "${temporary_directory}/${asset}"
fi
curl -fsSL "${release_url}/SHA256SUMS" -o "${temporary_directory}/SHA256SUMS"
expected="$(awk -v name="$asset" '$2 == name { print $1 }' "${temporary_directory}/SHA256SUMS")"
if [ -z "$expected" ]; then
  echo "platform-images: ${asset} is absent from SHA256SUMS" >&2
  exit 1
fi
if command -v sha256sum >/dev/null 2>&1; then
  actual="$(sha256sum "${temporary_directory}/${asset}" | awk '{ print $1 }')"
else
  actual="$(shasum -a 256 "${temporary_directory}/${asset}" | awk '{ print $1 }')"
fi
if [ "$actual" != "$expected" ]; then
  echo "platform-images: checksum verification failed for ${asset}" >&2
  exit 1
fi

tar -xzf "${temporary_directory}/${asset}" -C "$temporary_directory" platform
mkdir -p "$install_directory"
install -m 0755 "${temporary_directory}/platform" "${install_directory}/platform"
echo "Installed platform-images v${release_version} to ${install_directory}/platform"
case ":${PATH}:" in
  *":${install_directory}:"*) ;;
  *) echo "Add ${install_directory} to PATH to run: platform images --help" ;;
esac

#!/usr/bin/env bash
set -euo pipefail

# Override this only when deliberately installing a different release or main.
release_ref="${CODEX_ACCOUNTS_REF:-v1.0.0}"
destination="${CODEX_HOME:-$HOME/.codex}"

if ! command -v python3 >/dev/null || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    echo 'Python 3.10 or later is required.' >&2
    exit 1
fi
case "$(uname -s)" in
    Linux|Darwin) ;;
    *) echo 'Supported systems: Linux, macOS, and WSL.' >&2; exit 1 ;;
esac
if [[ $# -gt 1 || (${1:-} != '' && ${1:-} != '--local') ]]; then
    echo 'Usage: bash install.sh [--local]' >&2
    exit 1
fi
mkdir -p -m 700 "$destination"
staged_file="$(mktemp "$destination/.codex-accounts-install.XXXXXX")"
trap 'rm -f -- "$staged_file"' EXIT

if [[ ${1:-} == '--local' ]]; then
    source_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    cp -- "$source_directory/codex-accounts" "$staged_file"
else
    curl --fail --silent --show-error --location \
        "https://raw.githubusercontent.com/rik11112/codex-accounts/$release_ref/codex-accounts" \
        --output "$staged_file"
fi
# A failed or invalid download must leave the installed executable intact.
python3 -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$staged_file"
chmod 755 "$staged_file"
mv -f -- "$staged_file" "$destination/codex-accounts"
echo "Installed: $destination/codex-accounts"
echo 'Saved accounts and auth.json were left in place.'
echo "Run: $destination/codex-accounts --version"

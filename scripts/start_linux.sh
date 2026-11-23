#!/usr/bin/env bash
# One Lot Desk: Linux launcher. Shares its body with the macOS one.
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/start_mac.sh" "$@"

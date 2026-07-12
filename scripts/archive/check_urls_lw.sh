#!/usr/bin/env bash
# Check URLs from LiquidWeb server
set -euo pipefail

OUTFILE="/tmp/url_results.txt"
rm -f "$OUTFILE"

check_url() {
    local url="$1"
    local result
    result=$(curl -s -o /dev/null -w "%{http_code}|%{url_effective}|%{num_redirects}" --connect-timeout 10 --max-time 25 -L "$url" 2>&1) || result="ERROR|$url|0"
    echo "$url|$result" >> "$OUTFILE"
}
export -f check_url

# Read URLs from stdin, run 8 in parallel
xargs -P 8 -I {} bash -c 'check_url "$@"' _ {} < /dev/stdin

echo "Done. Results in $OUTFILE"

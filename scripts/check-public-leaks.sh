#!/bin/sh
set -eu

pattern='/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+|[A-Za-z0-9._%+-]+@(?!(?:example\.invalid)\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}'

if rg --text --pcre2 -n --hidden "$pattern" \
    -g '!.git/**' \
    -g '!scripts/check-public-leaks.sh'; then
    echo "Found a private home path or email address in publishable files." >&2
    exit 1
else
    scan_status=$?
    if [ "$scan_status" -ne 1 ]; then
        echo "Cannot scan the public working tree for private paths or email addresses." >&2
        exit 1
    fi
fi

history_matches="$(mktemp)"
trap 'rm -f "$history_matches"' EXIT HUP INT TERM

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Cannot scan public history: this directory is not a Git work tree." >&2
    exit 1
fi

if ! git rev-parse --verify HEAD^{commit} >/dev/null 2>&1; then
    echo "Cannot scan public history: HEAD is not a readable commit." >&2
    exit 1
fi

history_revisions="$(mktemp)"
trap 'rm -f "$history_matches" "$history_revisions"' EXIT HUP INT TERM
if ! git rev-list --all >"$history_revisions"; then
    echo "Cannot enumerate commits reachable from local refs." >&2
    exit 1
fi
if [ ! -s "$history_revisions" ]; then
    echo "Cannot scan public history: no commits are reachable from local refs." >&2
    exit 1
fi

while IFS= read -r revision; do
    if git grep --text -n -P "$pattern" "$revision" -- . \
        ':(exclude)scripts/check-public-leaks.sh' >>"$history_matches"; then
        :
    else
        history_status=$?
        if [ "$history_status" -ne 1 ]; then
            echo "Cannot scan commit $revision for public-boundary leaks." >&2
            exit 1
        fi
    fi

    symlink_matches="$(git ls-tree -r "$revision" | awk '$1 == "120000" { print }')"
    if [ -n "$symlink_matches" ]; then
        printf '%s\n' "$symlink_matches"
        echo "Found a symbolic link in public history; review and publish regular files instead." >&2
        exit 1
    fi

    while IFS= read -r tracked_path; do
        case "$tracked_path" in
            docs/assets/property-inventory-demo.gif)
                if ! git cat-file blob "$revision:$tracked_path" | python3 -c '
import struct
import sys

payload = sys.stdin.buffer.read()
if len(payload) > 2_000_000 or payload[:6] not in {b"GIF87a", b"GIF89a"}:
    raise SystemExit(1)
if len(payload) < 10 or struct.unpack("<HH", payload[6:10]) not in {(960, 560), (960, 520)}:
    raise SystemExit(1)
'; then
                    echo "$revision:$tracked_path"
                    echo "The approved README GIF has an unexpected format, size, or dimensions." >&2
                    exit 1
                fi
                continue
                ;;
            .gitignore|LICENSE|docs/assets/demo-search.jq|docs/assets/demo-status.jq|docs/assets/demo.tape|*.csv|*.geojson|*.json|*.jsonl|*.lock|*.md|*.py|*.sh|*.sql|*.svg|*.toml|*.yaml|*.yml)
                ;;
            *)
                echo "$revision:$tracked_path"
                echo "Found a binary-capable or unreviewed file type in public history." >&2
                exit 1
                ;;
        esac
        if ! git cat-file blob "$revision:$tracked_path" | python3 -c '
import sys

payload = sys.stdin.buffer.read()
if b"\0" in payload:
    raise SystemExit(1)
try:
    payload.decode("utf-8", errors="strict")
except UnicodeDecodeError:
    raise SystemExit(1)
'; then
            echo "$revision:$tracked_path"
            echo "Found binary or non-UTF-8 content disguised as a publishable text file." >&2
            exit 1
        fi
    done <<EOF
$(git ls-tree -r --name-only "$revision")
EOF
done <"$history_revisions"

if [ -s "$history_matches" ]; then
    cat "$history_matches"
    echo "Found a private home path or email address in a commit reachable from a local ref." >&2
    exit 1
fi

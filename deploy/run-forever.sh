#!/usr/bin/env bash
# Keep the bot alive without systemd, for hosts that only run a container or a
# plain shell. Restarts on crash, and on exit code 2 (bad config) backs off
# instead of hammering the API in a loop.
set -u

cd "$(dirname "$0")/.." || exit 1

PYTHON="${PYTHON:-.venv/bin/python}"
LOG="${LOG:-data/bot-runner.log}"
mkdir -p "$(dirname "$LOG")"

echo "$(date -Is) runner starting" >>"$LOG"

failures=0
while true; do
    start=$(date +%s)
    "$PYTHON" main.py >>"$LOG" 2>&1
    code=$?
    ran=$(( $(date +%s) - start ))

    if [ "$code" -eq 0 ] || [ "$code" -eq 130 ]; then
        echo "$(date -Is) clean exit ($code), stopping" >>"$LOG"
        exit 0
    fi

    # A run that lasted a while means it died of something transient, so the
    # failure count resets - otherwise a bot that works for hours and is then
    # killed a few times would eventually be given up on. Only quick deaths in a
    # row count as "the config is wrong, stop trying".
    if [ "$ran" -ge 60 ]; then
        failures=0
        backoff=5
    else
        failures=$((failures + 1))
        if [ "$failures" -ge 6 ]; then
            echo "$(date -Is) failed $failures times in a row, giving up" >>"$LOG"
            exit "$code"
        fi
        backoff=$((failures * 10))
    fi

    echo "$(date -Is) exit $code after ${ran}s, restart in ${backoff}s" >>"$LOG"
    sleep "$backoff"
done

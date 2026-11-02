#!/usr/bin/env bash
# Keep the bot alive on a host that has no systemd (a container, a plain shell).
#
# This is a real watchdog, not a restart wrapper. The difference matters: a
# process that is *hung* still has a PID, so "wait for it to exit" never fires
# and the user gets silence with no error anywhere. The bot writes a heartbeat
# every 30s; if that file goes stale, this script kills the process and starts
# a fresh one.
#
# Exit codes from main.py:
#   0   clean exit (Ctrl+C)        -> stop, do not restart
#   130 interrupted                -> stop, do not restart
#   2   missing config             -> stop; a restart cannot fix it
#   4   another bot owns the token -> stop; retrying would only fight it
set -u

cd "$(dirname "$0")/.." || exit 1

PYTHON="${PYTHON:-.venv/bin/python}"
LOG="${LOG:-data/bot-runner.log}"
HEARTBEAT="${HEARTBEAT:-data/bot.heartbeat}"
STALE_AFTER="${STALE_AFTER:-120}"   # 4x the 30s heartbeat: slow, but not dead
mkdir -p "$(dirname "$LOG")" "$(dirname "$HEARTBEAT")"

log() { echo "$(date -Is) $*" >>"$LOG"; }

log "watchdog starting (stale-after ${STALE_AFTER}s)"
failures=0

while true; do
    rm -f "$HEARTBEAT"
    "$PYTHON" main.py >>"$LOG" 2>&1 &
    pid=$!
    start=$(date +%s)

    # Watch the heartbeat while the bot runs. A stale file means the event loop
    # is no longer turning, which is a hang that only a restart can clear.
    while kill -0 "$pid" 2>/dev/null; do
        sleep 15
        if [ ! -f "$HEARTBEAT" ]; then
            continue   # still starting up
        fi
        age=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT" 2>/dev/null || echo 0) ))
        if [ "$age" -gt "$STALE_AFTER" ]; then
            log "heartbeat stale ${age}s: killing hung bot $pid"
            kill "$pid" 2>/dev/null
            sleep 5
            kill -9 "$pid" 2>/dev/null
            break
        fi
    done

    wait "$pid"
    code=$?
    ran=$(( $(date +%s) - start ))

    case "$code" in
        0|130)
            log "clean exit ($code), stopping"
            exit 0
            ;;
        2|4)
            log "fatal configuration exit ($code), not restarting"
            exit "$code"
            ;;
    esac

    # A run that lasted a while means it died of something transient, so the
    # failure count resets. Only quick deaths in a row mean "the config is
    # wrong, stop trying" - otherwise a bot that works for hours and is then
    # killed a few times would eventually be given up on.
    if [ "$ran" -ge 60 ]; then
        failures=0
        backoff=5
    else
        failures=$((failures + 1))
        if [ "$failures" -ge 6 ]; then
            log "failed $failures times in a row, giving up"
            exit "$code"
        fi
        backoff=$((failures * 10))
    fi

    log "exit $code after ${ran}s, restart in ${backoff}s"
    sleep "$backoff"
done

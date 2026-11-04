#!/bin/sh
#Claude Code hook: light the clock when a session needs you (Notification) or a turn fails (StopFailure).
#In ~/.claude/settings.json, run it from both hooks: "command": "/path/to/claude-code-hook.sh waiting" or "... error".
#The folder name says which session. Backgrounded with a short timeout, so a missing clock never slows Claude.
json=$(head -c 65536)
cwd=$(printf '%s' "$json" | grep -o '"cwd" *: *"[^"]*"' | head -n 1 | sed 's/^.*: *"//; s/"$//')
name=$(basename "${cwd:-claude}" | tr -cd 'A-Za-z0-9 ._&+-' | cut -c1-24)
case "${1:-waiting}" in
    waiting) look='"color":"Darkorange","anim":"Pulse","sound":"Bells"' ;;
    *) look='"color":"Red","anim":"Wave","sound":"Radar"' ;;
esac
curl -s -m 3 -XPOST "${GLANCE_URL:-http://127.0.0.1:8740}/notify" -d '{"text":"'"$name"'",'"$look"',"priority":"critical"}' >/dev/null 2>&1 &
exit 0

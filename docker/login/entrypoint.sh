#!/bin/bash
set -euo pipefail
umask 077

export DISPLAY=:99
export PROMOTION_LOGIN_MODE=remote
export PROMOTION_BROWSER_CHANNEL=chromium

# Validate before any port is opened. Missing or weak credentials fail closed.
python /app/docker/login/configure.py
nginx -t -c /tmp/promotion-login-nginx.conf
mkdir -p "${PROMOTION_SESSION_DIR:-/app/data/sessions}/authorization" \
         "${PROMOTION_RUNTIME_DIR:-/app/.runtime}"

pids=()
cleanup() {
    trap - EXIT INT TERM
    if ((${#pids[@]})); then
        kill "${pids[@]}" 2>/dev/null || true
        wait "${pids[@]}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

Xvfb :99 -screen 0 1600x1000x24 -nolisten tcp -ac &
pids+=("$!")
ready=0
for attempt in {1..50}; do
    if xdpyinfo -display :99 >/dev/null 2>&1; then ready=1; break; fi
    sleep 0.1
done
if [[ "$ready" != 1 ]]; then echo "授权桌面显示服务未就绪" >&2; exit 1; fi

openbox --sm-disable &
pids+=("$!")
x11vnc -display :99 -forever -shared -localhost -rfbport 5900 -nopw -noxdamage -quiet &
pids+=("$!")
websockify 127.0.0.1:6080 127.0.0.1:5900 &
pids+=("$!")
python /app/pipeline/control_panel.py --port 18761 &
pids+=("$!")
nginx -c /tmp/promotion-login-nginx.conf -g 'daemon off;' &
pids+=("$!")

# Do not leave a partly working authorization service running after a child dies.
set +e
wait -n "${pids[@]}"
status=$?
set -e
if [[ "$status" == 0 ]]; then status=1; fi
exit "$status"

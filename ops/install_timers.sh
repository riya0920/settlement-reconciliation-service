#!/bin/bash
# Install the daily reconciliation DAG as a real systemd timer.
#
#     sudo ops/install_timers.sh /path/to/se2-cycle-platform
#
# `run_cycle.py` runs the real pipeline through a DAG with dependencies, retries
# and an SLA. Nothing invoked it. The README argued a scheduler embedded in the
# application is one nobody can inspect, pause or back-fill from -- that
# argument is right and this stays OUTSIDE the application.
#
# Persistent=false, for the same reason DATA-1's reconciliation timer is:
# settlement state is CUMULATIVE. A later date processed before an earlier one
# applies its transitions to a book that never received the earlier day's, and
# the run finishes and reports success. Catching up is `catch_up()` in
# src/scheduler.py -- oldest-first, and it STOPS at the first failure rather
# than stepping over it, which a timer firing once per missed window cannot do.
#
# 20 = the file had not arrived and it is still inside the cutoff window. That
# is a wait, not an incident, and the unit lists it as a success -- paging at
# 09:00 for a file that usually lands at 10:00 trains an operator to close the
# alert unread.
set -euo pipefail

REPO="${1:-}"
if [ -z "$REPO" ] || [ ! -f "$REPO/run_cycle.py" ]; then
  echo "usage: $0 /path/to/se2-cycle-platform" >&2
  exit 2
fi

PY="${PYTHON:-python3}"
if ! "$PY" -c 'import fastapi' >/dev/null 2>&1; then
  for cand in /mnt/c/Python314/python.exe /mnt/c/Python313/python.exe /mnt/c/Python312/python.exe; do
    if [ -x "$cand" ] && "$cand" -c 'import fastapi' >/dev/null 2>&1; then
      PY="$cand"; break
    fi
  done
fi
if ! "$PY" -c 'import fastapi' >/dev/null 2>&1; then
  echo "no interpreter with fastapi found; set PYTHON=..." >&2
  echo "Refusing to install a timer that would fail on every fire." >&2
  exit 3
fi
echo "using interpreter: $PY"

# A Windows interpreter needs a Windows path, and systemd treats backslash in
# ExecStart as an escape -- so forward slashes.
SCRIPT="$REPO/run_cycle_tick.py"
case "$PY" in
  *.exe) SCRIPT="$(wslpath -w "$REPO/run_cycle_tick.py" | tr '\\' '/')" ;;
esac

cat > /etc/systemd/system/se2-cycle.service <<EOF
[Unit]
Description=SE-2 daily settlement cycle
Documentation=file://$REPO/README.md

[Service]
Type=oneshot
WorkingDirectory=$REPO
ExecStart=$PY $SCRIPT
# 20 = the DAG ran and the SLA was breached. A real signal, not a crash: the
# run produced correct output and took too long, and a unit marked failed for
# that trains an operator to ignore the colour.
SuccessExitStatus=0 20
EOF

cat > /etc/systemd/system/se2-cycle.timer <<'EOF'
[Unit]
Description=Run the DATA-1 reconciliation after the settlement cutoff

[Timer]
# After the 18:00 cutoff the cycle itself uses. Firing before it would make
# every run report "still inside the window", which is true and useless.
OnCalendar=*-*-* 18:30:00
# NOT persistent -- see the header. Settlement state is cumulative and catching
# up is run_backfill.py's job, oldest-first, resumable and subject to approval.
Persistent=false
RandomizedDelaySec=120
AccuracySec=1min
Unit=se2-cycle.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now se2-cycle.timer
echo "installed. next run:"
systemctl list-timers se2-cycle.timer --no-pager

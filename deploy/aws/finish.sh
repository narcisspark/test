#!/bin/bash
set -euo pipefail
source /etc/trading-paper/run.env
export RUN_ID DEADLINE RESULT_BUCKET AWS_DEFAULT_REGION
if [ -f "/var/lib/trading-paper/runs/${RUN_ID}/complete.json" ]; then
  # Leave the retained EBS disk intact even if an S3 retry fails.
  for attempt in 1 2 3; do
    if /bin/bash /opt/trading-paper/deploy/aws/backup.sh; then
      shutdown -h +1 'Seven-day paper trading run completed and archived'
      exit 0
    fi
    sleep 10
  done
  echo 'Final backup failed; retained EBS and scheduled EC2 stop remain available' >&2
  exit 1
fi

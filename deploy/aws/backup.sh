#!/bin/bash
set -euo pipefail
exec 9>/var/lib/trading-paper/backup.lock
flock 9
aws s3 sync /var/lib/trading-paper/ "s3://${RESULT_BUCKET}/runs/${RUN_ID}/" \
  --region "$AWS_DEFAULT_REGION" --exclude '*.tmp' --exclude '*.jsonl' \
  --exclude 'backup.log' --exclude '*.lock' --only-show-errors

#!/bin/bash
set -euo pipefail
exec > >(tee -a /var/log/trading-paper-bootstrap.log | logger -t trading-paper-bootstrap) 2>&1
dnf install -y python3.11 python3.11-pip
if ! command -v aws >/dev/null; then dnf install -y awscli-2; fi
id -u paper >/dev/null 2>&1 || useradd --system --home-dir /var/lib/trading-paper --shell /sbin/nologin paper
install -d -m 755 /opt/trading-paper /etc/trading-paper
install -d -m 750 -o paper -g paper /var/lib/trading-paper
aws s3 cp 's3://__BUCKET__/__RELEASE_KEY__' /tmp/paper-release.tar.gz --region __REGION__ --only-show-errors
tar -xzf /tmp/paper-release.tar.gz -C /opt/trading-paper
python3.11 -m venv /opt/trading-paper/venv
/opt/trading-paper/venv/bin/pip install --disable-pip-version-check /opt/trading-paper
python3.11 - <<'PY'
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
deadline = (datetime.now(timezone.utc) + timedelta(days=7)).replace(microsecond=0).isoformat()
values = {'RUN_ID': '__RUN_ID__', 'DEADLINE': deadline, 'RESULT_BUCKET': '__BUCKET__', 'AWS_DEFAULT_REGION': '__REGION__'}
Path('/etc/trading-paper/run.env').write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
Path('/var/lib/trading-paper/cloud.json').write_text(json.dumps({'run_id': values['RUN_ID'], 'deadline': deadline, 'region': '__REGION__', 'bucket': '__BUCKET__', 'duration_days': 7}))
PY
chown root:paper /etc/trading-paper/run.env
chmod 640 /etc/trading-paper/run.env
cp /opt/trading-paper/deploy/aws/paper-*.service /etc/systemd/system/
cp /opt/trading-paper/deploy/aws/paper-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now paper-backup.timer
systemctl enable --now paper-trading.service
echo 'Paper trading bootstrap complete'

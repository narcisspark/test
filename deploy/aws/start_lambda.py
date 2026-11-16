#!/usr/bin/env python3
"""Validate the initial live quote, then enable minute polling with a fixed end."""
import base64
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
directory = ROOT / 'outputs/aws' / sys.argv[1]
manifest_path = directory / 'deployment.json'
manifest = json.loads(manifest_path.read_text())


def aws(*parts):
    result = subprocess.run(['aws', '--profile', manifest['profile'], '--region', manifest['region'], *parts, '--output', 'json'], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout.strip() else {}


response_path = directory / 'initial-invoke.json'
response = aws('lambda', 'invoke', '--function-name', manifest['FunctionName'], '--cli-binary-format', 'raw-in-base64-out', '--payload', '{}', '--log-type', 'Tail', str(response_path))
if response.get('LogResult'):
    (directory / 'initial-invoke.log').write_bytes(base64.b64decode(response['LogResult']))
result = json.loads(response_path.read_text())
if response.get('FunctionError') or result.get('status') != 'running' or result.get('rest_quote_samples', 0) < 1:
    raise RuntimeError(f'Initial invocation failed; schedule remains disabled: {result}')
meta_path = directory / 'run.json'
aws('s3api', 'get-object', '--bucket', manifest['Bucket'], '--key', manifest['ResultsPrefix'] + 'run.json', str(meta_path))
meta = json.loads(meta_path.read_text())
end = (datetime.fromisoformat(meta['deadline']) + timedelta(minutes=2)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
aws('cloudformation', 'update-stack', '--stack-name', manifest['stack'], '--use-previous-template', '--capabilities', 'CAPABILITY_IAM',
    '--parameters', json.dumps([{'ParameterKey': 'PollingEnabled', 'ParameterValue': 'ENABLED'}, {'ParameterKey': 'EndAtUTC', 'ParameterValue': end}]))
print('Initial quote received and checkpoint archived; enabling minute schedule', flush=True)
aws('cloudformation', 'wait', 'stack-update-complete', '--stack-name', manifest['stack'])
manifest.update({'stack_status': 'UPDATE_COMPLETE', 'simulation_running': True, 'schedule_enabled': True,
                 'started_at': meta['started_at'], 'deadline': meta['deadline'], 'schedule_end_date': end,
                 'report_s3': 's3://' + manifest['Bucket'] + '/' + manifest['ResultsPrefix'] + 'report.md'})
manifest_path.write_text(json.dumps(manifest, indent=2))
for filename in ('report.json', 'report.md'):
    aws('s3api', 'get-object', '--bucket', manifest['Bucket'], '--key', manifest['ResultsPrefix'] + filename, str(directory / filename))
print(json.dumps({'started_at': meta['started_at'], 'deadline': meta['deadline'], 'function': manifest['FunctionName'], 'report': manifest['report_s3']}, indent=2), flush=True)

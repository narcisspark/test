#!/usr/bin/env python3
"""Read current AWS state and save an auditable local report snapshot."""
import base64
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
directory = ROOT / 'outputs/aws' / sys.argv[1]
manifest = json.loads((directory / 'deployment.json').read_text())


def aws(*parts, missing_ok=False):
    result = subprocess.run(['aws', '--profile', manifest['profile'], '--region', manifest['region'], *parts, '--output', 'json'], capture_output=True, text=True)
    if result.returncode:
        if missing_ok and 'ResourceNotFoundException' in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout.strip() else {}


jobs = {
    'configuration': lambda: aws('lambda', 'get-function-configuration', '--function-name', manifest['FunctionName'], '--query', '{State:State,LastUpdateStatus:LastUpdateStatus,MemorySize:MemorySize,Timeout:Timeout,Architectures:Architectures,Runtime:Runtime,CodeSha256:CodeSha256}'),
    'schedule': lambda: aws('scheduler', 'get-schedule', '--name', manifest['ScheduleName'], missing_ok=True),
    'report': lambda: aws('s3api', 'get-object', '--bucket', manifest['Bucket'], '--key', manifest['ResultsPrefix'] + 'report.json', str(directory / 'report.json')),
    'markdown': lambda: aws('s3api', 'get-object', '--bucket', manifest['Bucket'], '--key', manifest['ResultsPrefix'] + 'report.md', str(directory / 'report.md')),
    'logs': lambda: aws('logs', 'filter-log-events', '--log-group-name', '/aws/lambda/' + manifest['FunctionName'], '--start-time', str(int(datetime.fromisoformat(manifest['started_at']).timestamp() * 1000)), '--query', 'events[].message')
}
with ThreadPoolExecutor(max_workers=5) as pool:
    futures = {name: pool.submit(job) for name, job in jobs.items()}
    results = {name: future.result() for name, future in futures.items()}
report = json.loads((directory / 'report.json').read_text())
config, schedule = results['configuration'], results['schedule']
now = datetime.now(timezone.utc)
errors = [line for line in results['logs'] if any(term in line for term in ('[ERROR]', 'Task timed out', 'Traceback'))]
summary = {'checked_at': now.isoformat(), 'function_state': config['State'], 'code_hash_matches_release': base64.b64decode(config['CodeSha256']).hex() == manifest['release_sha256'],
           'schedule_state': schedule['State'] if schedule else 'deleted', 'schedule_expression': schedule['ScheduleExpression'] if schedule else None,
           'status': report['status'], 'started_at': report['started_at'], 'deadline': report['deadline'], 'report_as_of': report['as_of'],
           'report_age_seconds': round((now - datetime.fromisoformat(report['as_of'])).total_seconds(), 2),
           'rest_quote_samples': report['rest_quote_sample_count'], 'completed_candles': report['completed_polled_candle_count'],
           'last_quote_age_seconds': report['last_quote_age_seconds'], 'latest_quotes': report['latest_quotes'],
           'fills': {key: value['fills_count'] for key, value in report['portfolios'].items()},
           'runtime': config, 'cloud_errors': errors, 'recent_duration_lines': [line for line in results['logs'] if line.startswith('REPORT')][-5:]}
(directory / 'cloud-validation.json').write_text(json.dumps(summary, indent=2))
(directory / 'cloudwatch.log').write_text(''.join(results['logs']))
print(json.dumps(summary, indent=2))
if not summary['code_hash_matches_release'] or config['State'] != 'Active' or errors:
    raise SystemExit('Cloud validation failed; inspect cloud-validation.json')
if report['status'] == 'running' and (not schedule or schedule['State'] != 'ENABLED' or summary['report_age_seconds'] > 180):
    raise SystemExit('The minute schedule is not healthy; inspect cloud-validation.json')

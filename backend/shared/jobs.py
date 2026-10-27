"""Durable job snapshots for the single-process local workbench."""

from datetime import datetime, timezone
import json
import re

from backend.shared.artifacts import save_json


ACTIVE_STATUSES = {'queued', 'running', 'stopping'}
_JOB_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
INTERRUPTED_MESSAGE = (
    'The backend stopped before this job finished. Saved findings are preserved. '
    'Use Resume failed run when available, or start a new run if extraction did not finish.'
)


def job_path(output, job_id):
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise ValueError('Invalid job identifier')
    return output / 'jobs' / (job_id + '.json')


def save_job(output, job_id, job, *, update_latest=True):
    """Keep each job's own logs even after a later run replaces the PDF logs."""
    path = job_path(output, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {key: value for key, value in job.items() if key != 'pdf_path'}
    record['id'] = job_id
    save_json(path, record)
    if update_latest:
        state = {key: record.get(key) for key in (
            'id', 'pdf_id', 'status', 'error', 'started_at', 'updated_at', 'finished_at',
            'action', 'candidate_id', 'options', 'resume', 'interrupted')}
        save_json(output / 'run_state.json', state)


def read_job(output, job_id):
    try:
        record = json.loads(job_path(output, job_id).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if (not isinstance(record, dict) or record.get('id') != job_id
            or not isinstance(record.get('logs'), list)
            or any(not isinstance(row, dict) for row in record['logs'])):
        return None
    return record


def interrupt_job(record):
    """No registered worker remains; never imply that persisted work is running."""
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    record.update(status='failed', interrupted=True, error=INTERRUPTED_MESSAGE,
                  finished_at=now, updated_at=now)
    entry = {'at': now, 'level': 'error', 'message': INTERRUPTED_MESSAGE}
    if isinstance(record.get('request_progress'), dict):
        progress = {**record['request_progress'], 'status': 'interrupted',
                    'provider_status': 'Backend stopped; completion of the in-flight provider request is unknown'}
        record['request_progress'] = progress
        entry['progress'] = progress
    record.setdefault('logs', []).append(entry)
    return entry

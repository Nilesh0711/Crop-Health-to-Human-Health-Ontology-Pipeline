"""Keep the previous scientific artifacts before an explicitly requested fresh run."""
from datetime import datetime, timezone
from pathlib import Path
import shutil
import uuid


def archive_run(output):
    output = Path(output)
    files = [p for p in output.iterdir() if p.is_file() and p.suffix in {'.json','.jsonl','.graphml','.txt'}] if output.exists() else []
    if not files:
        return None
    label = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    destination = output.parent / '.historical_runs' / output.name / label
    destination.mkdir(parents=True)
    for path in files:
        shutil.copy2(path,destination / path.name)
    return destination

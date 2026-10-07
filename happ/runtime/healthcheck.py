import json,time
from pathlib import Path
try:
 s=json.loads(Path('/state/status.json').read_text())
 raise SystemExit(0 if s['healthy'] and time.time()-s['updated_at']<90 else 1)
except (ValueError,OSError,KeyError):
 raise SystemExit(1)

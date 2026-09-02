import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# www/, so `import otmlib` works from a checkout the same way it does in Docker.
_WWW = _ROOT.parent
for _path in (_ROOT, _WWW):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

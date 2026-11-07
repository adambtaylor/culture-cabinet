"""Load the shared pure scope contract without importing Django or owner helpers."""
import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / 'backend' / 'shelf' / 'episode_scope.py'
_spec = importlib.util.spec_from_file_location('_cabinet_episode_scope', _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
parse_scope = _module.parse_scope
episode_code = _module.episode_code
make_scope = _module.make_scope
is_episode_scope = _module.is_episode_scope

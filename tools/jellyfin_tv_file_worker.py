"""One selected TV file on the destination host, through a trusted parent channel.

No SSH, credential bootstrap, listener, source deletion or status mutation. The
parent must supervise the actual authority deadline and reconcile inspect first.
"""
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from shelf import completion_registry as registry
from shelf.jellyfin_tv_import import CopyPlan, receive_selected

MAX_CONTROL = 64 * 1024


def run(operation, source):
    try:
        registry.require(operation in ('inspect', 'receive'))
        line = source.readline(MAX_CONTROL + 1)
        registry.require(type(line) is bytes and len(line) <= MAX_CONTROL and line.endswith(b'\n'))
        control = json.loads(line, object_pairs_hook=registry.unique_object)
        registry.require(type(control) is dict and set(control) == set(CopyPlan.__dataclass_fields__))
        result = receive_selected(CopyPlan(**control), source, operation=operation)
        output = {'status': result.status, 'reason': result.reason,
                  'receipt': asdict(result.receipt) if result.receipt else None}
        registry.require(len(registry.encode_json(output)) <= MAX_CONTROL)
        return output
    except Exception:
        return {'status': 'unknown', 'reason': 'selected_worker_unavailable', 'receipt': None}


if __name__ == '__main__':
    operation = sys.argv[1].removeprefix('--') if len(sys.argv) == 2 else None
    # Unbuffered input avoids waiting on the pipe while a buffered reader has
    # already prefetched file bytes beyond the bounded control frame.
    output = run(operation, sys.stdin.buffer.raw)
    sys.stdout.buffer.write(registry.encode_json(output))
    raise SystemExit(0 if output['status'] == 'verified' else 1)

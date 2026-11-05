"""Submit the fit job through mrun, shipping a frozen copy of the worker and strength module.

MRUN_URL=http://127.0.0.1:19025 ~/domains/common/bin/common exec python submit.py --name fit-1
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
STRENGTH = HERE.parents[1] / "src" / "refimpact" / "strength.py"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--ram-mb", type=int, default=6000)
    ap.add_argument("--timeout-s", type=int, default=600)
    ap.add_argument("--priority", type=int, default=70)
    a = ap.parse_args()
    # The upload happens after preflight, so ship a copy nothing else edits.
    payload = Path(tempfile.mkdtemp(prefix=f"refimpact-{a.name}-"))
    for source in (HERE / "worker.py", HERE / "run.sh", STRENGTH):
        shutil.copy(source, payload / source.name)
    shas = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in payload.iterdir()}
    print("payload", payload, shas, flush=True)
    from mrun.client.api import Api
    from mrun.client.submit import submit

    job = submit(
        experiment=f"ref-impact-{a.name}",
        cmd=["bash", "run.sh", a.name],
        payload=payload,
        needs={"cuda": False},
        host="beast",
        reservation={"ram_mb": a.ram_mb, "vram_mb": 0, "cpu_threads": 4},
        timeout_s=a.timeout_s,
        priority=a.priority,
        model="ref-impact-fit",
        config={"task_family": "ref-impact-fit", "source_sha256": shas},
        note=f"ref-impact {a.name}: first-down and this-drive chance fit, 2024-2026, CPU only",
        retry_on_kill=False,
        api=Api(timeout_s=60),
        preflight=True,
    )
    print(job, flush=True)


if __name__ == "__main__":
    main()

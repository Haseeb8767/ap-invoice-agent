"""Run every sample invoice through the pipeline and compare with the expected decision.

Usage:  python scripts/run_samples.py            (uses a throwaway database)
"""
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STAGE_DELAY", "0")
os.environ["RUNTIME_DIR"] = tempfile.mkdtemp(prefix="ap-test-")

from app import config, db, pipeline  # noqa: E402

db.conn()
manifest = json.loads((config.SAMPLES_DIR / "manifest.json").read_text())
print(f"Extraction mode: {config.extraction_mode()}\n")
ok = 0
for s in manifest:
    src = config.SAMPLES_DIR / s["file"]
    dest = config.UPLOAD_DIR / s["file"]
    shutil.copy(src, dest)
    run_id = "t-" + uuid.uuid4().hex[:6]  # run synchronously (no background thread)
    db.create_run(run_id, s["file"], dest, "test", config.extraction_mode())
    pipeline._run(run_id, dest, s["file"])
    run = db.get_run(run_id)
    got = run["decision"]
    expected = s["expected_decision"]
    mark = "PASS" if got == expected else "FAIL"
    ok += got == expected
    codes = [i["code"] for i in (run["result"] or {}).get("issues", [])]
    print(f"[{mark}] {s['file']:<48} expected={expected:<8} got={got:<8} {codes}")
    print(f"       {run['headline']}")
print(f"\n{ok}/{len(manifest)} scenarios produced the expected decision")

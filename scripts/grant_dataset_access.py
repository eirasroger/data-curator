"""Grant dataset-level BigQuery access.

`bq add-iam-policy-binding` on a dataset returns "This feature requires
allowlisting" on ordinary projects, and because the setup script tolerated the
failure the grants silently did not happen. The portable way is the dataset's
own access list: read it, add entries, write it back.

Dataset roles map to the familiar ones: READER = dataViewer, WRITER =
dataEditor, OWNER = dataOwner.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

PROJECT = os.environ.get("PROJECT", "data-curator-507614")
DATASET = os.environ.get("DATASET", "curator")
BQ = shutil.which("bq") or shutil.which("bq.cmd")

GRANTS = {
    f"curator-worker@{PROJECT}.iam.gserviceaccount.com": "WRITER",  # the only writer
    f"curator-api@{PROJECT}.iam.gserviceaccount.com": "READER",     # read only
}

meta = json.loads(subprocess.run(
    [BQ, "show", "--format=prettyjson", f"{PROJECT}:{DATASET}"],
    capture_output=True, text=True, check=True).stdout)

access = meta.get("access", [])
existing = {(a.get("userByEmail"), a.get("role")) for a in access}

added = []
for email, role in GRANTS.items():
    if (email, role) not in existing:
        access.append({"role": role, "userByEmail": email})
        added.append(f"{role:<7} {email}")

if not added:
    print("all grants already present")
    sys.exit(0)

meta["access"] = access
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
    json.dump(meta, fh)
    path = fh.name

result = subprocess.run([BQ, "update", "--source", path, f"{PROJECT}:{DATASET}"],
                        capture_output=True, text=True)
os.unlink(path)
print(result.stdout.strip() or result.stderr.strip())
for line in added:
    print(f"  granted {line}")
sys.exit(result.returncode)

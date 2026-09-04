# Shared settings. Source this, don't run it:  . scripts/gcp_env.sh
#
# The bq CLI is a Python program, and under Git Bash on Windows it fails with
# "python3.14: command not found" because it cannot resolve an interpreter on
# its own. The Cloud SDK ships one; point at it explicitly. Harmless elsewhere.
if [ -z "${CLOUDSDK_PYTHON:-}" ] && command -v gcloud >/dev/null 2>&1; then
  _sdk_root="$(dirname "$(dirname "$(command -v gcloud)")")"
  _bundled="$_sdk_root/platform/bundledpython/python.exe"
  if [ -x "$_bundled" ]; then
    if command -v cygpath >/dev/null 2>&1; then
      CLOUDSDK_PYTHON="$(cygpath -w "$_bundled")"
    else
      CLOUDSDK_PYTHON="$_bundled"
    fi
    export CLOUDSDK_PYTHON
  fi
fi

export PROJECT="${PROJECT:-data-curator-507614}"
export REGION="${REGION:-europe-west1}"       # Belgium: closest EU region with everything we need
export BQ_LOCATION="${BQ_LOCATION:-EU}"       # keep the data in the EU
export DATASET="${DATASET:-curator}"
export TOPIC="${TOPIC:-epd-changes}"
export DLQ_TOPIC="${DLQ_TOPIC:-epd-changes-dlq}"
export SUBSCRIPTION="${SUBSCRIPTION:-epd-changes-worker}"

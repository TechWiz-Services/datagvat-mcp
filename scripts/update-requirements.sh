#!/bin/sh
# Re-resolve the dependencies declared in pyproject.toml and rewrite the
# hash-pinned requirements files. Runs in a throwaway python:3.12-alpine
# container (needs network access to PyPI; installs pip-tools inside it).
#
# After running: review the diff, rebuild the image (tests run during the
# build) and smoke-test before committing.
set -eu
cd "$(dirname "$0")/.."

docker run --rm -v "$PWD:/src" -w /src python:3.12-alpine sh -euc '
  pip install -q --root-user-action=ignore --disable-pip-version-check pip-tools
  pip-compile -q --generate-hashes --strip-extras --no-header -o requirements.txt pyproject.toml
  printf "pytest\npytest-asyncio\n-c requirements.txt\n" > /tmp/dev.in
  pip-compile -q --generate-hashes --strip-extras --no-header -o requirements-dev.txt /tmp/dev.in
'
echo "Updated requirements.txt and requirements-dev.txt"

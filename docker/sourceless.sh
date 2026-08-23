#!/bin/sh
# Replace a tree's .py files with bytecode, in place. Used twice in
# docker/Dockerfile — once for the Pro MCP server in `pydeps`, once for
# /app/src in `worker` — which is why it is a script and not two RUN blocks
# that have to be kept in agreement.
#
#   sourceless.sh /app/src
#
# `compileall -b` writes each module's .pyc to the LEGACY location (beside the
# source, not in __pycache__/), which is the only layout CPython imports from
# once the .py is gone.
#
# --invalidation-mode unchecked-hash, not the default timestamp mode: timestamp
# .pyc files embed the source's mtime, which changes every build and would churn
# the layer even when nothing changed. It also suits sourceless imports, where
# there is no source left to validate against.
#
# Compiled with the venv interpreter, not the base image's: the .pyc magic
# number must match the interpreter that actually runs the app.
#
# PYTHON_SOURCELESS=0 keeps the sources — useful when debugging a traceback
# inside a running container, where sourceless frames show no code.
set -eu

dir="$1"

if [ "${PYTHON_SOURCELESS:-1}" != "1" ]; then
    echo "PYTHON_SOURCELESS=0 — keeping Python sources in ${dir}"
    exit 0
fi

/opt/venv/bin/python -m compileall -q -b --invalidation-mode unchecked-hash "$dir"
find "$dir" -name '*.py' -delete
find "$dir" -type d -name '__pycache__' -prune -exec rm -rf {} +
echo "bytecode-only ${dir}: $(find "$dir" -name '*.pyc' | wc -l) .pyc, $(find "$dir" -name '*.py' | wc -l) .py"

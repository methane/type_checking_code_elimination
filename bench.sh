#!/bin/bash
PYVER=${PYVER:-3.14}
rm -rf .venv
uv venv -p $PYVER
uv pip install --compile-bytecode ./sqlalchemy-2.0.39
/usr/bin/time -l hyperfine -w1 '.venv/bin/python -c "import sqlalchemy"'
uv run sum_pyc_size.py .venv/lib/python$PYVER/site-packages/sqlalchemy


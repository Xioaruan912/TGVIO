#!/bin/sh
set -eu

export PYTHONPATH="${PYTHONPATH:-$(pwd)/src}"
python -m tgvio.main --check
python -m unittest discover -s tests -v


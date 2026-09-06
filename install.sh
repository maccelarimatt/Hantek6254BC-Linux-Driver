#!/bin/sh
set -eu

project_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
printf 'Rebuilding the virtual environment with %s (%s)\n' \
    "$(command -v python3)" "$(python3 -c 'import platform; print(platform.python_version())')"
python3 -m venv --clear "$project_dir/.venv"
"$project_dir/.venv/bin/python" -m pip install --upgrade pip
"$project_dir/.venv/bin/pip" install --upgrade "$project_dir[plot]"
install -m 0755 "$project_dir/scripts/hantek6254bc" "$project_dir/.venv/bin/hantek6254bc"
"$project_dir/.venv/bin/hantek6254bc" --help >/dev/null
printf '\nInstalled. Check the connected scope with:\n  %s/.venv/bin/hantek6254bc doctor\n' "$project_dir"

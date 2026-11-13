#!/bin/sh
cd "$(dirname "$0")" || exit 1
python3 start.py "$@"
result=$?
if [ "$result" -ne 0 ]; then
  printf '\nStartup failed. Install Python 3.9+ and check the error above.\n'
  read -r answer
fi
exit "$result"

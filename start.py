#!/usr/bin/env python3
"""Set up a private virtual environment and start PerfBoard (standard library only)."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request
import venv
import webbrowser

ROOT = Path(__file__).resolve().parent


def open_when_ready(url):
    for _ in range(60):
        try:
            with urllib.request.urlopen(url + '/api/runs', timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except OSError:
            time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description='Set up and launch PerfBoard without Whisper')
    parser.add_argument('--setup-only', action='store_true', help='Install dependencies without starting the server')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--db', default=str(ROOT / 'data' / 'perfboard.sqlite3'))
    args = parser.parse_args()
    if sys.version_info < (3, 9):
        parser.error('Python 3.9 or newer is required (Python 3.11 recommended).')
    environment = ROOT / '.venv'
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.is_file():
        print('Creating the PerfBoard Python environment...', flush=True)
        venv.create(environment, with_pip=True)
    digest = hashlib.sha256()
    for name in ('setup.cfg', 'pyproject.toml', 'setup.py'):
        digest.update((ROOT / name).read_bytes())
    fingerprint = digest.hexdigest()
    marker = environment / '.perfboard-setup'
    if not marker.is_file() or marker.read_text() != fingerprint:
        print('Installing PerfBoard dependencies (first run requires internet)...', flush=True)
        # System Python on macOS can ship pip 21, before PEP 660 editable installs.
        subprocess.run([str(python), '-m', 'pip', 'install', '--upgrade', 'pip'], check=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '-e', str(ROOT)], check=True)
        marker.write_text(fingerprint)
    if args.setup_only:
        print('PerfBoard is ready. Run this script again to launch.')
        return
    url = f'http://127.0.0.1:{args.port}'
    print(f'Opening {url} — press Ctrl+C to stop.', flush=True)
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()
    command = [str(python), '-c', 'from record2mcap.perfboard_server import cli_main; cli_main()',
               '--port', str(args.port), '--db', str(Path(args.db).expanduser().resolve())]
    try:
        subprocess.run(command, cwd=ROOT, check=True)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as error:
        print('PerfBoard could not start. Check the error above and retry.', file=sys.stderr)
        sys.exit(error.returncode)

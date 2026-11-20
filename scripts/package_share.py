"""Export only distributable source, never local runs or Git history."""
import argparse
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    'README.md', 'AGENTS.md', '.gitignore', 'pyproject.toml', 'setup.cfg',
    'setup.py', 'requirements.txt', 'foxglove.example.json',
    'start.py', 'start.command', 'start.bat', 'scripts/package_share.py',
    'start-whisper.bat', 'requirements-whisper-windows.txt',
)
DIRECTORIES = ('record2mcap', 'tests', 'docs', 'skills', '.github')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--windows-whisper', action='store_true')
    windows = parser.parse_args().windows_whisper
    name = 'PncPerfBoard-Windows-Whisper' if windows else 'PerfBoard'
    destination = ROOT / 'dist' / name
    # Only refresh this script's own named export directory.
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    paths = [ROOT / name for name in FILES]
    for directory in DIRECTORIES:
        paths.extend(p for p in (ROOT / directory).rglob('*') if p.is_file()
                     and p.suffix in {'.py', '.cjs', '.css', '.html', '.js', '.md', '.yml'})
    for source in paths:
        target = destination / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    archive = ROOT / 'dist' / (f'{name}.zip' if windows else 'PerfBoard-source.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as out:
        for path in sorted(destination.rglob('*')):
            if path.is_file():
                out.write(path, Path(destination.name) / path.relative_to(destination))
    print(f'Exported {len(paths)} source files to {archive}')


if __name__ == '__main__':
    main()

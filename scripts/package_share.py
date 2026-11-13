"""Export only distributable source, never local runs or Git history."""
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    'README.md', 'AGENTS.md', '.gitignore', 'pyproject.toml', 'setup.cfg',
    'setup.py', 'requirements.txt', 'foxglove.example.json',
    'start.py', 'start.command', 'start.bat', 'scripts/package_share.py',
)
DIRECTORIES = ('record2mcap', 'tests', 'docs', 'skills', '.github')


def main():
    destination = ROOT / 'dist' / 'PerfBoard'
    # Only refresh this script's own named export directory.
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    paths = [ROOT / name for name in FILES]
    for name in DIRECTORIES:
        paths.extend(p for p in (ROOT / name).rglob('*') if p.is_file()
                     and '__pycache__' not in p.parts and p.suffix != '.pyc')
    for source in paths:
        target = destination / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    archive = ROOT / 'dist' / 'PerfBoard-source.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as out:
        for path in sorted(destination.rglob('*')):
            if path.is_file():
                out.write(path, Path('PerfBoard') / path.relative_to(destination))
    print(f'Exported {len(paths)} source files to {archive}')


if __name__ == '__main__':
    main()

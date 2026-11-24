#!/usr/bin/env python3
"""Build the installable Ubuntu package from the B6ing source tree."""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


PACKAGING = Path(__file__).resolve().parent
PROJECT = PACKAGING.parents[1]
sys.path.insert(0, str(PROJECT))

from cinnado_live import __version__
from cinnado_live.desktop import APP_ID, APP_LAUNCHER_ICON, ICON_SEARCH_PATH, packaged_launcher


def install_file(source: Path, destination: Path, mode: int = 0o644):
    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(mode)


def build(output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    package = output / f'b6ing_{__version__}_all.deb'
    with TemporaryDirectory(prefix='b6ing-deb-', dir=output) as temporary:
        staging = Path(temporary) / 'package'
        application = staging / 'usr/lib/b6ing'
        source = PROJECT / 'cinnado_live'
        code = (*source.glob('*.py'), *source.glob('*.css'), *(source / 'vendor_r2').glob('*.py'),
                source / 'vendor_r2/LICENSE', source / 'vendor_r2/UPSTREAM.md')
        for file in code:
            install_file(file, application / file.relative_to(PROJECT))
        for file in ICON_SEARCH_PATH.rglob('*.svg'):
            install_file(file, application / file.relative_to(PROJECT))
        install_file(APP_LAUNCHER_ICON, staging / 'usr/share/icons/hicolor/scalable/apps' / APP_LAUNCHER_ICON.name)
        install_file(PACKAGING / 'b6ing', application / 'b6ing', mode=0o755)

        binary = staging / 'usr/bin/b6ing'
        binary.parent.mkdir(parents=True)
        binary.symlink_to('../lib/b6ing/b6ing')

        launcher = staging / 'usr/share/applications' / f'{APP_ID}.desktop'
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(packaged_launcher(), encoding='utf-8')
        launcher.chmod(0o644)

        installed_size = sum((file.stat().st_size + 1023) // 1024 for file in staging.rglob('*') if file.is_file())
        control = staging / 'DEBIAN/control'
        control.parent.mkdir(mode=0o755)
        control.write_text((PACKAGING / 'control').read_text(encoding='utf-8').format(
            version=__version__, installed_size=installed_size,
        ), encoding='utf-8')
        control.chmod(0o644)
        for directory in (staging, *(path for path in staging.rglob('*') if path.is_dir())):
            directory.chmod(0o755)

        archive = Path(temporary) / package.name
        subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(staging), str(archive)], check=True)
        archive.chmod(0o644)
        archive.replace(package)
    return package


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=PROJECT / 'dist')
    options = parser.parse_args()
    print(build(options.output_dir.resolve()))

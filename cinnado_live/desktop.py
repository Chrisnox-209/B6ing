"""B6ing launchers for the application menu and desktop sign-in."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

LEGACY_APP_ID = 'io.github.wansviewubuntu.CinnadoLive'
APP_ID = 'io.github.wansviewubuntu.B6ing'
APP_NAME = 'B6ing'
SOURCE = Path(__file__).resolve().parent.parent
APP_ICON_NAME = 'b6ing-eye-symbolic'
ICON_SEARCH_PATH = SOURCE / 'assets' / 'icons'
APP_ICON = ICON_SEARCH_PATH / 'hicolor' / 'scalable' / 'apps' / f'{APP_ICON_NAME}.svg'
APP_LAUNCHER_ICON = APP_ICON.with_name('b6ing-launcher.svg')


def desktop_quote(value: str) -> str:
    value = value.replace("%", "%%")
    return '"' + ''.join('\\' + c if c in '\\"\x60$' else c for c in value) + '"'


def write_launcher(path: Path, *, minimized: bool = False) -> Path:
    python = SOURCE / '.venv/bin/python'
    if not python.is_file():
        raise FileNotFoundError('L’environnement Python de B6ing est introuvable.')
    command = f'{desktop_quote(str(python))} -m cinnado_live'
    if minimized:
        command += ' --minimized'
    content = '\n'.join([
        '[Desktop Entry]', 'Version=1.0', 'Type=Application', f'Name={APP_NAME}',
        'Comment=Voir les caméras solaires Cinnado B6 sur Ubuntu',
        f'Exec={command}', f'Path={SOURCE}', f'Icon={APP_LAUNCHER_ICON}',
        'Terminal=false', 'Categories=AudioVideo;Video;', 'Keywords=Be Seeing;Wansview;Cinnado;Caméra;',
        f'StartupWMClass={APP_ID}', f'StartupNotify={str(not minimized).lower()}', '',
    ])
    return _write_entry(path, content)


def _write_entry(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                            prefix='.b6ing-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def autostart_entry() -> Path:
    config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    return config / 'autostart' / f'{APP_ID}.desktop'


def autostart_enabled() -> bool:
    path = autostart_entry()
    return path.is_file() or path.with_name(f'{LEGACY_APP_ID}.desktop').is_file()


def set_autostart(enabled: bool):
    path = autostart_entry()
    if enabled:
        write_launcher(path, minimized=True)
    else:
        path.unlink(missing_ok=True)
    path.with_name(f'{LEGACY_APP_ID}.desktop').unlink(missing_ok=True)


def migrate_autostart() -> None:
    path = autostart_entry()
    legacy = path.with_name(f'{LEGACY_APP_ID}.desktop')
    if not legacy.is_file():
        return
    if not path.exists():
        content = legacy.read_text(encoding='utf-8')
        keys = {'Name': APP_NAME, 'Icon': str(APP_LAUNCHER_ICON), 'StartupWMClass': APP_ID}
        lines = []
        for line in content.splitlines():
            key = line.partition('=')[0]
            lines.append(f'{key}={keys[key]}' if key in keys else line)
        _write_entry(path, '\n'.join(lines) + '\n')
    legacy.unlink()

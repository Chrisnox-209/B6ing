"""Persistent local camera names and media destinations."""

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile


def _local_folder(value) -> Path | None:
    if isinstance(value, str) and '\x00' not in value:
        folder = Path(value)
        if folder.is_absolute():
            return folder
    return None


def _media_path(folder, default_folder, name, index, started, extension):
    if folder is None:
        folder = default_folder
        folder.mkdir(parents=True, exist_ok=True)
    basename = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '-', name)
    basename = basename.encode('utf-8')[:180].decode('utf-8', errors='ignore').strip(' .-')
    if not basename:
        basename = f'Caméra {index}'
    return folder / f'{basename}_{started:%Y-%m-%d_%H-%M-%S}.{extension}'


@dataclass(frozen=True)
class Preferences:
    recording_folder: Path | None = None
    camera_names: dict[str, str] = field(default_factory=dict)
    photo_folder: Path | None = None
    motion_notifications: bool = True

    @classmethod
    def load(cls, path: Path) -> 'Preferences':
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        names = data.get('camera_names')
        return cls(
            recording_folder=_local_folder(data.get('recording_folder')),
            photo_folder=_local_folder(data.get('photo_folder')),
            motion_notifications=data.get('motion_notifications') is not False,
            camera_names={key: name.strip()[:70] for key, name in names.items()
                          if isinstance(name, str) and name.strip()} if isinstance(names, dict) else {},
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {
            'recording_folder': str(self.recording_folder) if self.recording_folder is not None else None,
            'photo_folder': str(self.photo_folder) if self.photo_folder is not None else None,
            'camera_names': self.camera_names,
            'motion_notifications': self.motion_notifications,
        }
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                prefix='.settings-', delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.close()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

    def recording_path(self, default_folder: Path, name: str, index: int, started: datetime) -> Path:
        return _media_path(self.recording_folder, default_folder, name, index, started, 'mkv')

    def photo_path(self, default_folder: Path, name: str, index: int, captured: datetime) -> Path:
        return _media_path(self.photo_folder, default_folder, name, index, captured, 'png')

    def alert_clip_path(self, default_folder: Path, name: str, index: int, occurred: datetime, extension: str) -> Path:
        path = _media_path(self.recording_folder, default_folder, name, index, occurred, extension)
        return path.with_stem(f'{path.stem}_alerte')

"""Local motion alert history, kept separately for each signed-in account."""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from tempfile import NamedTemporaryFile


MAX_ALERTS = 200


@dataclass(frozen=True)
class AlertEntry:
    camera_id: str
    identifier: str
    camera_name: str
    occurred_at: datetime
    unread: bool = True

    @property
    def key(self) -> tuple[str, str]:
        return self.camera_id, self.identifier


class AlertJournal:
    def __init__(self, path: Path):
        self.path = path
        self.entries, self.imported_cameras = self._load()

    @classmethod
    def for_account(cls, directory: Path, email: str) -> 'AlertJournal':
        account = sha256(email.strip().casefold().encode('utf-8')).hexdigest()
        return cls(directory / f'{account}.json')

    @property
    def unread_count(self) -> int:
        return sum(entry.unread for entry in self.entries)

    def _load(self) -> tuple[tuple[AlertEntry, ...], frozenset[str]]:
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return (), frozenset()
        if not isinstance(data, dict):
            return (), frozenset()
        imported = data.get('imported_cameras')
        cameras = frozenset()
        if isinstance(imported, list):
            cameras = frozenset(camera for camera in imported if isinstance(camera, str))
        rows = data.get('entries')
        if not isinstance(rows, list):
            return (), cameras
        entries = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            camera, identifier, name, date = (row.get(field) for field in
                                               ('camera_id', 'identifier', 'camera_name', 'occurred_at'))
            if not all(isinstance(value, str) and value for value in (camera, identifier, name, date)):
                continue
            try:
                occurred_at = datetime.fromisoformat(date)
            except ValueError:
                continue
            if occurred_at.tzinfo is not None:
                entry = AlertEntry(camera, identifier, name, occurred_at, unread=row.get('unread') is not False)
                entries.setdefault(entry.key, entry)
        return self._latest(entries.values()), cameras

    @staticmethod
    def _latest(entries: Iterable[AlertEntry]) -> tuple[AlertEntry, ...]:
        return tuple(sorted(entries, key=lambda entry: entry.occurred_at, reverse=True)[:MAX_ALERTS])

    def add(self, entry: AlertEntry) -> bool:
        if any(old.key == entry.key for old in self.entries):
            return False
        entries = self._latest((entry, *self.entries))
        self._save(entries)
        return entry in entries

    def import_history(self, camera_id: str, entries: Iterable[AlertEntry]) -> None:
        if camera_id in self.imported_cameras:
            return
        combined = {entry.key: replace(entry, unread=False) for entry in entries}
        combined.update((entry.key, entry) for entry in self.entries)
        self._save(self._latest(combined.values()), imported_cameras=self.imported_cameras | {camera_id})

    def mark_read(self) -> None:
        self._save(tuple(replace(entry, unread=False) if entry.unread else entry for entry in self.entries))

    def remove(self, key: tuple[str, str]) -> None:
        self._save(tuple(entry for entry in self.entries if entry.key != key))

    def clear(self, camera_ids: Iterable[str] = ()) -> None:
        self._save((), imported_cameras=self.imported_cameras.union(camera_ids))

    def _save(self, entries: tuple[AlertEntry, ...], *, imported_cameras: frozenset[str] | None = None) -> None:
        if imported_cameras is None:
            imported_cameras = self.imported_cameras
        if entries == self.entries and imported_cameras == self.imported_cameras:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = [{
            'camera_id': entry.camera_id, 'identifier': entry.identifier, 'camera_name': entry.camera_name,
            'occurred_at': entry.occurred_at.isoformat(), 'unread': entry.unread,
        } for entry in entries]
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                prefix='.alerts-', delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump({'entries': data, 'imported_cameras': sorted(imported_cameras)}, stream, ensure_ascii=False)
                stream.close()
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
        self.entries = entries
        self.imported_cameras = imported_cameras

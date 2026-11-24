#!/usr/bin/env python3
"""Ajouter B6ing au menu Applications du compte courant."""

import argparse
import os
from pathlib import Path
import shutil

from cinnado_live.desktop import APP_ID, APP_ICON, LEGACY_APP_ID, migrate_autostart, write_launcher
from cinnado_live.desktop import desktop_quote as desktop_quote


def install_cinnado_live() -> Path:
    if os.geteuid() == 0:
        raise SystemExit("Installe B6ing depuis le compte qui l’utilisera.")
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    try:
        entry = write_launcher(base / "applications" / f"{APP_ID}.desktop")
    except FileNotFoundError:
        raise SystemExit("Crée d’abord l’environnement Python décrit dans le README.")

    icons = base / "icons/hicolor/scalable/apps"
    icons.mkdir(parents=True, exist_ok=True)
    shutil.copy2(APP_ICON, icons / APP_ICON.name)
    migrate_autostart()
    entry.with_name(f"{LEGACY_APP_ID}.desktop").unlink(missing_ok=True)
    return entry


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cinnado-live", action="store_true", help=argparse.SUPPRESS)
    parser.parse_args()
    print(f"B6ing : {install_cinnado_live()}")

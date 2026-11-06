"""Router: walks inbox/ and turns what it finds into InputItems.

Session rules (one lecture = one session):
  * A folder with a session.yaml is one session. The sidecar may list `files:` explicitly.
  * A folder with exactly one video plus one or more audio files is one session
    (the video + separate recorder case) - no sidecar needed.
  * Any other media file is its own single-file session. A .srt/.vtt with the same stem
    is attached to it.
  * Two audio-only recordings of the same lecture need a session.yaml, because nothing
    else distinguishes them from two different lectures.
Non-media files inside a session folder (e.g. the slide PDF) are routed on their own,
before the session, so the session can link to them.

URLs come from .url / .webloc files and from urls.txt (or *.urls.txt): one URL per line,
lines starting with # are ignored.
"""
from __future__ import annotations

import fnmatch
import plistlib
import re
from configparser import ConfigParser
from pathlib import Path

import yaml

from .ingestors.base import (AUDIO_EXT, IGNORE_NAMES, SUBTITLE_EXT, URL_EXT, VIDEO_EXT,
                             InputItem)

URL_RE = re.compile(r"^https?://\S+$", re.I)


def _rel(p: Path, inbox: Path) -> str:
    return p.relative_to(inbox).as_posix()


def _read_sidecar(folder: Path) -> dict:
    for name in ("session.yaml", "session.yml"):
        f = folder / name
        if f.exists():
            return yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return {}


def _urls_from_file(f: Path) -> list[str]:
    suffix = f.suffix.lower()
    if suffix == ".webloc":
        try:
            return [plistlib.loads(f.read_bytes())["URL"]]
        except Exception:
            return []
    if suffix == ".url":
        cp = ConfigParser(interpolation=None)
        try:
            cp.read_string(f.read_text(encoding="utf-8", errors="ignore"))
            return [cp["InternetShortcut"]["URL"]]
        except Exception:
            return []
    lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
    return [ln.strip() for ln in lines if URL_RE.match(ln.strip())]


def _is_url_list(f: Path) -> bool:
    return f.suffix.lower() in URL_EXT or f.name.lower() == "urls.txt" or f.name.lower().endswith(".urls.txt")


def discover(inbox: Path, ignore: list[str] | None = None, skip=None) -> list[InputItem]:
    """skip: an optional predicate for paths the caller owns (a library in place skips its own files)."""
    inbox = Path(inbox)
    ignore = list(ignore or [])

    def skipped(p: Path) -> bool:
        return any(fnmatch.fnmatch(p.name, pat) for pat in ignore) or bool(skip and skip(p))
    files_first: list[InputItem] = []
    sessions: list[InputItem] = []
    urls: list[InputItem] = []
    seen_urls: set[str] = set()

    def visit(folder: Path) -> None:
        entries = sorted(p for p in folder.iterdir() if not p.name.startswith(".") and not skipped(p))
        files = [p for p in entries if p.is_file() and p.name not in IGNORE_NAMES]
        subdirs = [p for p in entries if p.is_dir()]
        sidecar = _read_sidecar(folder)
        media = [p for p in files if p.suffix.lower() in AUDIO_EXT | VIDEO_EXT]
        subs = [p for p in files if p.suffix.lower() in SUBTITLE_EXT]
        videos = [p for p in media if p.suffix.lower() in VIDEO_EXT]
        audios = [p for p in media if p.suffix.lower() in AUDIO_EXT]
        claimed: set[Path] = set()

        if sidecar or (len(videos) == 1 and audios):
            listed = sidecar.get("files")
            members = [folder / f for f in listed] if listed else media + subs
            missing = [m for m in members if not m.exists()]
            if missing:
                raise FileNotFoundError(f"session.yaml in {folder} lists missing files: {missing}")
            sessions.append(InputItem(kind="session", key=_rel(folder, inbox) + "/",
                                      paths=members, sidecar=sidecar, folder=folder))
            claimed.update(members)
        else:
            for m in media:
                attached = [s for s in subs if s.stem == m.stem]
                sessions.append(InputItem(kind="session", key=_rel(m, inbox), paths=[m, *attached],
                                          folder=folder))
                claimed.update([m, *attached])

        for f in files:
            if f in claimed:
                continue
            if _is_url_list(f):
                for u in _urls_from_file(f):
                    if u not in seen_urls:
                        seen_urls.add(u)
                        urls.append(InputItem(kind="url", key=u, url=u, paths=[f], folder=folder))
            elif f.suffix.lower() not in SUBTITLE_EXT:
                extra = {}
                if sidecar.get("slides") == f.name:
                    session_title = sidecar.get("title") or folder.name.replace("_", " ").replace("-", " ")
                    extra = {"title": f"{session_title} (slides)"}
                files_first.append(InputItem(kind="file", key=_rel(f, inbox), paths=[f], folder=folder,
                                             sidecar=extra))

        for d in subdirs:
            visit(d)

    if inbox.exists():
        visit(inbox)
    return files_first + urls + sessions

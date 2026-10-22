from __future__ import annotations

import os
from pathlib import Path

import google.auth
from google.auth.transport.requests import AuthorizedSession

SCOPES = ["https://www.googleapis.com/auth/adwords"]

CREDENTIALS_ENV = "GOOGLE_ADS_CREDENTIALS"
ENV_FILE_ENV = "GOOGLE_ADS_ENV_FILE"
DEFAULT_ENV_FILES = (
    Path.home() / ".config" / "google-ads" / ".env",
    Path(".env"),
)

_session: AuthorizedSession | None = None
_env_files_loaded = False


def load_env_files() -> None:
    global _env_files_loaded
    if _env_files_loaded:
        return
    _env_files_loaded = True
    override = os.environ.get(ENV_FILE_ENV)
    candidates = (Path(override).expanduser(),) if override else DEFAULT_ENV_FILES
    for candidate in candidates:
        try:
            content = candidate.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for line in content.splitlines():
            entry = line.strip().removeprefix("export ").strip()
            if not entry or entry.startswith("#") or "=" not in entry:
                continue
            name, _, value = entry.partition("=")
            os.environ.setdefault(name.strip(), value.strip().strip("\"'"))
        return


def get_session() -> AuthorizedSession:
    global _session
    if _session is None:
        load_env_files()
        path = os.environ.get(CREDENTIALS_ENV)
        if path:
            credentials, _ = google.auth.load_credentials_from_file(path, scopes=SCOPES)
        else:
            credentials, _ = google.auth.default(scopes=SCOPES)
        _session = AuthorizedSession(credentials)
    return _session

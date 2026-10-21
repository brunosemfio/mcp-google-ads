from __future__ import annotations

import google.auth
from google.auth.transport.requests import AuthorizedSession

SCOPES = ["https://www.googleapis.com/auth/adwords"]

_session: AuthorizedSession | None = None


def get_session() -> AuthorizedSession:
    global _session
    if _session is None:
        credentials, _ = google.auth.default(scopes=SCOPES)
        _session = AuthorizedSession(credentials)
    return _session

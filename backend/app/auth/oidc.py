"""Integration seam for Microsoft Entra ID (OIDC). Not enabled in the MVP.

If enabled later, the callback must: validate the ID token signature against the tenant's JWKS,
verify ``iss`` equals https://login.microsoftonline.com/<tenant-id>/v2.0, verify ``aud`` equals the
registered client ID, check ``exp``/``nbf``/``nonce``, map the ``oid`` claim to a local user, and
require an existing membership row before creating a session. Group claims must never grant
tenant access by themselves.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    client_id: str
    audience: str
    jwks_uri: str


def get_config() -> OIDCConfig | None:
    return None  # not configured in this release

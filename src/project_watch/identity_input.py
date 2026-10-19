"""Validate human entity reviews without fetching or trusting cited content."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class VerifiedIdentityInput:
    legal_name: str
    registration_code: str | None
    evidence_source_id: str | None
    evidence_locator: str | None
    evidence_url: str | None
    note: str


def review_note(value: str) -> str:
    note = " ".join(value.split()) if isinstance(value, str) else ""
    if not 5 <= len(note) <= 500:
        raise ValueError("identity review note must contain 5-500 characters")
    return note


def _public_https_reference(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1000:
        raise ValueError("identity evidence URL must be a public HTTPS reference")
    if any(char.isspace() or ord(char) < 32 or char == "\\" for char in value):
        raise ValueError("identity evidence URL must be a public HTTPS reference")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        raise ValueError("identity evidence URL must be a public HTTPS reference") from exc
    if (
        parsed.scheme != "https"
        or not host
        or "." not in host
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port != 443)
        or host in {"localhost", "localhost.localdomain"}
        or host.endswith((".local", ".internal", ".test"))
    ):
        raise ValueError("identity evidence URL must be a public HTTPS reference")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return value
    raise ValueError("identity evidence URL must be a public HTTPS reference")


def verified_identity_input(
    *,
    legal_name: str,
    registration_code: str | None,
    evidence_source_id: str | None,
    evidence_locator: str | None,
    evidence_url: str | None,
    note: str,
) -> VerifiedIdentityInput:
    name = " ".join(legal_name.split()) if isinstance(legal_name, str) else ""
    if not 2 <= len(name) <= 200:
        raise ValueError("legal entity name must contain 2-200 characters")
    code = " ".join(registration_code.split()) if registration_code else None
    if code is not None and not 3 <= len(code) <= 60:
        raise ValueError("registration code must contain 3-60 characters")
    source_id = evidence_source_id.strip() if evidence_source_id else None
    locator = evidence_locator.strip() if evidence_locator else None
    url = evidence_url.strip() if evidence_url else None
    if bool(source_id) != bool(locator) or bool(url) == bool(source_id):
        raise ValueError("identity review requires either a located source or an HTTPS reference")
    if locator is not None and len(locator) > 200:
        raise ValueError("identity evidence locator is too long")
    return VerifiedIdentityInput(
        name,
        code,
        source_id,
        locator,
        _public_https_reference(url) if url else None,
        review_note(note),
    )

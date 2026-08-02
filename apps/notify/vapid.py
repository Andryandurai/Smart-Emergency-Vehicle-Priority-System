"""VAPID key handling for Web Push.

VAPID (RFC 8292) is how a server identifies itself to a browser's push service
without holding an account with that service. SEVPS generates one keypair; the
public key goes to browsers at subscribe time, the private key signs each push.

The keypair is *deployment identity*, not a rotating secret. Rotating it
invalidates every existing subscription, because a push service binds a
subscription to the key that created it — every controller and every hospital
would have to re-grant notification permission, discovering this only when an
alert failed to arrive. So keys are read from settings or from a file on disk
and generated once, never regenerated implicitly.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

log = logging.getLogger("sevps.notify")

KEY_FILENAME = "vapid_private.pem"


def _b64(raw: bytes) -> str:
    """URL-safe base64 without padding, which is what the Push API expects."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_keypair() -> tuple[str, str]:
    """Return ``(private_pem, public_key_b64)`` for a fresh P-256 keypair."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return private_pem, _b64(public_raw)


def public_key_from_private(private_pem: str) -> str:
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
    return _b64(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
    )


def key_path() -> Path:
    configured = settings.SEVPS.get("VAPID_KEY_PATH", "")
    if configured:
        return Path(configured)
    return Path(settings.BASE_DIR) / "models" / KEY_FILENAME


def private_key_pem() -> str:
    """The signing key, from settings first and the key file second.

    Settings win so a container deployment can inject the key as an environment
    variable and keep the filesystem read-only.
    """
    from_settings = settings.SEVPS.get("VAPID_PRIVATE_KEY", "")
    if from_settings:
        # An env var carrying a PEM usually has literal "\n" rather than newlines.
        return from_settings.replace("\\n", "\n")
    path = key_path()
    if path.exists():
        return path.read_text(encoding="ascii")
    return ""


def public_key() -> str:
    explicit = settings.SEVPS.get("VAPID_PUBLIC_KEY", "")
    if explicit:
        return explicit
    private = private_key_pem()
    if not private:
        return ""
    try:
        return public_key_from_private(private)
    except Exception:
        log.exception("VAPID private key is present but unreadable")
        return ""


def is_configured() -> bool:
    return bool(private_key_pem())


def claims_configured() -> bool:
    return bool(settings.SEVPS.get("VAPID_SUBJECT", ""))


def claims() -> dict:
    """VAPID JWT claims. ``sub`` must be a contactable mailto: or https: URI —
    it is how a push service operator reaches the sender about abuse."""
    subject = settings.SEVPS.get("VAPID_SUBJECT", "")
    if not subject:
        raise ImproperlyConfigured(
            "SEVPS_VAPID_SUBJECT must be set to a mailto: or https: URI for Web Push."
        )
    return {"sub": subject}


def write_keypair(path: Path | None = None, *, overwrite: bool = False) -> tuple[Path, str]:
    """Generate and persist a keypair. Returns ``(path, public_key)``."""
    target = path or key_path()
    if target.exists() and not overwrite:
        raise FileExistsError(
            f"{target} already exists. Overwriting invalidates every existing "
            "push subscription; pass --force only if that is intended."
        )
    private_pem, public = generate_keypair()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(private_pem, encoding="ascii")
    try:
        target.chmod(0o600)  # best effort; a no-op on Windows
    except OSError:  # pragma: no cover
        pass
    return target, public

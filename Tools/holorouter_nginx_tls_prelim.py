#!/usr/bin/env python3
"""
Version 1.0 - 2026-04-27
Author - Burke Azbill and HOL Core Team
Prelim helper: queue holorouter nginx TLS renewal when auth.vcf.lab is near expiry.

Instead of SCP/SSH (often blocked), copies ``Tools/holorouter/renew-nginx-tls-from-vault.sh``
to the holorouter NFS share (``lsf.holorouter_dir``, e.g. /tmp/holorouter on manager) and
creates ``renew_nginx_tls.request``. On the router, ``/mnt/manager`` is the same share; the
watcher invokes ``Tools/doupdate.sh``, which runs the script and clears the drop files.
"""

from __future__ import annotations

import os
import shutil
import socket
import ssl
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional, Tuple

# Default: renew if auth.vcf.lab leaf expires in this many days or less (includes expired).
DEFAULT_RENEW_WITHIN_DAYS = 14
CHECK_HOST = "auth.vcf.lab"
CHECK_PORT = 443
RENEW_SCRIPT_NAME = "renew-nginx-tls-from-vault.sh"
REQUEST_FLAG = "renew_nginx_tls.request"


def inspect_tls_leaf(
    host: str = CHECK_HOST,
    port: int = CHECK_PORT,
    timeout: float = 12.0,
) -> Tuple[Optional[datetime], Optional[str]]:
    """
    Connect and read the presented TLS leaf ``notAfter``.

    Returns ``(not_after_utc, None)`` on success, or ``(None, diagnostic)`` on failure.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
    except OSError as exc:
        return None, f"socket/TLS error: {exc!s}"
    if not cert:
        return None, "empty peer certificate"
    na = cert.get("notAfter")
    if not na:
        return None, "certificate missing notAfter field"
    dt = parsedate_to_datetime(na)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc), None


def _leaf_not_after_utc(host: str, port: int, timeout: float) -> Optional[datetime]:
    """Return notAfter of the presented TLS leaf as timezone-aware UTC, or None."""
    end, _err = inspect_tls_leaf(host, port, timeout)
    return end


def days_until_leaf_expires(
    host: str = CHECK_HOST,
    port: int = CHECK_PORT,
    timeout: float = 12.0,
) -> Optional[int]:
    """
    Whole days from now until the TLS leaf ``notAfter`` (UTC floor).

    Negative if already expired. None if TLS could not be inspected.
    """
    end, _err = inspect_tls_leaf(host, port, timeout)
    if end is None:
        return None
    now = datetime.now(timezone.utc)
    return int((end - now).total_seconds() // 86400)


def renew_script_path(holroot: str) -> str:
    return os.path.join(holroot, "Tools", "holorouter", RENEW_SCRIPT_NAME)


def maybe_renew_holorouter_nginx_tls(
    lsf: Any,
    renew_within_days: int = DEFAULT_RENEW_WITHIN_DAYS,
    dry_run: bool = False,
) -> Tuple[bool, str]:
    """
    If ``auth.vcf.lab`` TLS expires within ``renew_within_days`` (or is expired), or if
    TLS inspection fails from the manager, copy the renewal script to ``lsf.holorouter_dir``
    and create ``renew_nginx_tls.request`` for ``doupdate.sh`` on the holorouter
    (reads ``/mnt/manager``). Fail-open on inspection failure so the router can still
    refresh PEMs when the manager cannot complete a handshake to ``auth.vcf.lab``.

    :param lsf: lsfunctions module (holroot, holorouter_dir, write_output)
    :param renew_within_days: renew when remaining whole days <= this value
    :param dry_run: log only, no files written
    :return: (success, short message for logs)
    """
    log = getattr(lsf, "write_output", print)
    hr_dir = getattr(lsf, "holorouter_dir", "/tmp/holorouter")

    log(
        f"holorouter TLS: checking {CHECK_HOST}:{CHECK_PORT} "
        f"(queue renewal on share when whole UTC days until notAfter <= {renew_within_days})"
    )

    not_after, diag = inspect_tls_leaf(CHECK_HOST, CHECK_PORT, 12.0)
    days: Optional[int] = None
    if not_after is None:
        log(
            f"holorouter TLS: inspection failed for {CHECK_HOST}:{CHECK_PORT} ({diag}); "
            "queuing renewal anyway (fail-open: holorouter can still refresh PEMs from Vault)"
        )
    else:
        now = datetime.now(timezone.utc)
        days = int((not_after - now).total_seconds() // 86400)
        log(
            f"holorouter TLS: leaf notAfter={not_after.isoformat()} "
            f"(~{days} whole UTC days from now; renew_within_days={renew_within_days})"
        )
        if days > renew_within_days:
            msg = (
                f"holorouter TLS: renewal not queued — {days}d remaining exceeds "
                f"{renew_within_days}d threshold"
            )
            log(msg)
            return True, msg

    local_script = renew_script_path(lsf.holroot)
    if not os.path.isfile(local_script):
        msg = f"holorouter TLS: renewal script missing: {local_script}"
        log(f"WARNING: {msg}")
        return False, msg

    dst_script = os.path.join(hr_dir, RENEW_SCRIPT_NAME)
    dst_flag = os.path.join(hr_dir, REQUEST_FLAG)

    if dry_run:
        if not_after is None:
            msg = (
                f"holorouter TLS: dry-run — would queue renewal for doupdate "
                f"(inspection failed: {diag} → {dst_script} + {REQUEST_FLAG})"
            )
        else:
            msg = (
                f"holorouter TLS: dry-run — would queue renewal for doupdate "
                f"(notAfter={not_after.isoformat()}, ~{days}d left → {dst_script} + {REQUEST_FLAG})"
            )
        log(msg)
        return True, msg

    try:
        os.makedirs(hr_dir, mode=0o775, exist_ok=True)
        shutil.copy2(local_script, dst_script)
        os.chmod(dst_script, 0o755)
        with open(dst_flag, "w", encoding="utf-8") as f:
            f.write("1\n")
    except OSError as e:
        msg = f"holorouter TLS: could not write NFS share {hr_dir}: {e}"
        log(f"WARNING: {msg}")
        return False, msg

    if not_after is None:
        msg = (
            f"holorouter TLS: queued nginx cert renewal for doupdate.sh "
            f"(inspection failed: {diag}); "
            f"on router see {REQUEST_FLAG} + {RENEW_SCRIPT_NAME} under NFS mount"
        )
    else:
        msg = (
            f"holorouter TLS: queued nginx cert renewal for doupdate.sh "
            f"(notAfter={not_after.isoformat()}, ~{days}d left); "
            f"on router see {REQUEST_FLAG} + {RENEW_SCRIPT_NAME} under NFS mount"
        )
    log(msg)
    return True, msg

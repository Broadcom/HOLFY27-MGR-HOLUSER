#!/usr/bin/env python3
"""
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


def _leaf_not_after_utc(host: str, port: int, timeout: float) -> Optional[datetime]:
    """Return notAfter of the presented TLS leaf as timezone-aware UTC, or None."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
    except OSError:
        return None
    if not cert:
        return None
    na = cert.get("notAfter")
    if not na:
        return None
    dt = parsedate_to_datetime(na)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def days_until_leaf_expires(
    host: str = CHECK_HOST,
    port: int = CHECK_PORT,
    timeout: float = 12.0,
) -> Optional[int]:
    """
    Whole days from now until the TLS leaf ``notAfter`` (UTC floor).

    Negative if already expired. None if TLS could not be inspected.
    """
    end = _leaf_not_after_utc(host, port, timeout)
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
    If ``auth.vcf.lab`` TLS expires within ``renew_within_days`` (or is expired),
    copy the renewal script to ``lsf.holorouter_dir`` and create ``renew_nginx_tls.request``
    for ``doupdate.sh`` on the holorouter (reads ``/mnt/manager``).

    :param lsf: lsfunctions module (holroot, holorouter_dir, write_output)
    :param renew_within_days: renew when remaining whole days <= this value
    :param dry_run: log only, no files written
    :return: (success, short message for logs)
    """
    log = getattr(lsf, "write_output", print)
    hr_dir = getattr(lsf, "holorouter_dir", "/tmp/holorouter")

    days = days_until_leaf_expires()
    if days is None:
        msg = (
            f"holorouter TLS: could not inspect {CHECK_HOST}:{CHECK_PORT} "
            "(DNS/TLS unavailable); skipping nginx cert renewal queue"
        )
        log(msg)
        return True, msg

    if days > renew_within_days:
        msg = (
            f"holorouter TLS: {CHECK_HOST} leaf expires in {days} days "
            f"(>{renew_within_days}d threshold); skipping renewal queue"
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
        msg = (
            f"holorouter TLS: dry-run — would queue renewal for doupdate "
            f"({CHECK_HOST} in {days} days → {dst_script} + {REQUEST_FLAG})"
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

    msg = (
        f"holorouter TLS: queued nginx cert renewal for doupdate.sh ({CHECK_HOST} was in {days} days); "
        f"see router {REQUEST_FLAG} + {RENEW_SCRIPT_NAME} on NFS share"
    )
    log(msg)
    return True, msg

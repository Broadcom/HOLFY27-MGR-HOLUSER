#!/usr/bin/env python3
"""
Sync the active HashiCorp Vault PKI root CA into Firefox NSS profiles on the LMC.

Used by lab startup (prelim) so https://auth.vcf.lab, https://vault.vcf.lab, and
other Vault-signed endpoints trust after CA rotation. Mirrors the Vault step in
Tools/confighol-9.1.py without importing the full config script.

Download strategy:
  1. Try http://10.1.1.1:32000/v1/pki/ca/pem  — 3 attempts, 15 s between each.
  2. If all fail, try https://vault.vcf.lab/v1/pki/ca/pem — 4 attempts, 15 s between each.
  SSL certificate validation is intentionally disabled: the cert is issued to
  vault.vcf.lab, not the NodePort IP, so hostname verification would always fail.

Version: 1.2 (2026-05-06)
"""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable

# NodePort PKI on holorouter (same as confighol-9.1.py)
VAULT_CA_URL_IP   = "http://10.1.1.1:32000/v1/pki/ca/pem"
VAULT_CA_URL_FQDN = "https://vault.vcf.lab/v1/pki/ca/pem"
VAULT_CA_NICKNAME = "vcf.lab Root Authority"
CERTUTIL = "certutil"

# SSL context shared by all HTTPS fetches — hostname/cert validation disabled
# because the Vault cert is issued to vault.vcf.lab, not the NodePort IP.
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

LogFn = Callable[[str], None]


def _log_call(log: LogFn | None, msg: str) -> None:
    if log:
        log(msg)


def _fetch_with_retries(
    url: str,
    max_attempts: int,
    delay: int,
    timeout: int,
    log: LogFn | None,
) -> str | None:
    """
    Attempt to GET *url* up to *max_attempts* times, sleeping *delay* seconds
    between failures.  Returns the response body on the first valid PEM, or
    None if all attempts are exhausted.
    """
    # Only pass the SSL context for HTTPS; urllib silently ignores it for HTTP
    # but being explicit avoids any future confusion.
    ctx = _SSL_CTX if url.startswith("https://") else None

    for attempt in range(1, max_attempts + 1):
        try:
            kwargs: dict = {"timeout": timeout}
            if ctx is not None:
                kwargs["context"] = ctx
            with urllib.request.urlopen(url, **kwargs) as resp:
                body = resp.read().decode("utf-8", errors="replace").strip()
            if body.startswith("-----BEGIN CERTIFICATE-----"):
                return body
            _log_call(log, f"vault_firefox_trust: unexpected response from {url} (not a PEM)")
        except (urllib.error.URLError, OSError) as exc:
            _log_call(
                log,
                f"vault_firefox_trust: attempt {attempt}/{max_attempts} failed for {url}: {exc}",
            )

        if attempt < max_attempts:
            time.sleep(delay)

    return None


def _firefox_profile_dirs(mc_base: str) -> list[str]:
    """Return profile paths under the console home tree (e.g. /lmchol/... on manager)."""
    base = os.path.join(
        mc_base, "home", "holuser", "snap", "firefox", "common", ".mozilla", "firefox"
    )
    if not os.path.isdir(base):
        return []
    return [
        os.path.join(base, name)
        for name in os.listdir(base)
        if os.path.isdir(os.path.join(base, name))
        and os.path.isfile(os.path.join(base, name, "cert9.db"))
    ]


def download_vault_root_ca_pem(
    timeout: int = 15,
    log: LogFn | None = None,
) -> str | None:
    """
    Download the Vault PKI root CA PEM, trying the NodePort IP first and
    falling back to the FQDN if the IP is unreachable.
    """
    pem = _fetch_with_retries(VAULT_CA_URL_IP, max_attempts=3, delay=15, timeout=timeout, log=log)
    if pem:
        return pem

    _log_call(log, f"vault_firefox_trust: IP fetch failed; falling back to {VAULT_CA_URL_FQDN}")
    return _fetch_with_retries(VAULT_CA_URL_FQDN, max_attempts=4, delay=15, timeout=timeout, log=log)


def import_ca_pem_to_profile(
    ca_pem: str,
    profile_path: str,
    *,
    ca_name: str = VAULT_CA_NICKNAME,
    log: LogFn | None = None,
) -> bool:
    ca_file: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".pem", delete=False, encoding="utf-8"
        ) as f:
            f.write(ca_pem)
            ca_file = f.name
    except OSError as exc:
        _log_call(log, f"vault_firefox_trust: temp PEM write failed: {exc}")
        return False

    try:
        db = f"sql:{profile_path}"

        # Remove existing entry and any stale auto-renamed duplicates before importing
        for nick in [ca_name, f"{ca_name} #2", f"{ca_name} #3"]:
            probe = subprocess.run(
                [CERTUTIL, "-L", "-d", db, "-n", nick],
                capture_output=True,
            )
            if probe.returncode == 0:
                subprocess.run([CERTUTIL, "-D", "-d", db, "-n", nick], capture_output=True)

        imp = subprocess.run(
            [CERTUTIL, "-A", "-d", db, "-n", ca_name, "-t", "CT,,", "-i", ca_file],
            capture_output=True,
            text=True,
        )
        if imp.returncode != 0:
            _log_call(log, f"vault_firefox_trust: certutil import failed for {profile_path}: {imp.stderr.strip()}")
            return False
        return True
    finally:
        if ca_file:
            try:
                os.unlink(ca_file)
            except OSError:
                pass


def sync_vault_ca_to_firefox(
    mc_base: str,
    log: LogFn,
    dry_run: bool = False,
) -> bool:
    """
    Download Vault PKI root CA and import into every Firefox profile on the LMC.

    :param mc_base: Console home root as seen from manager (lsf.mc, typically /lmchol)
    :param log: callback for messages (e.g. lsf.write_output)
    :param dry_run: if True, only log intended actions
    :return: True if CA was synced to all found profiles, or nothing to do;
             False if profiles exist but import failed
    """
    if dry_run:
        log("vault_firefox_trust: dry-run — would sync Vault PKI root CA to Firefox")
        return True

    if not shutil.which(CERTUTIL):
        log("vault_firefox_trust: certutil not installed (apt install libnss3-tools); skipping")
        return True

    ca_pem = download_vault_root_ca_pem(log=log)
    if not ca_pem:
        log(
            f"vault_firefox_trust: could not download Vault CA from {VAULT_CA_URL_IP} "
            f"or {VAULT_CA_URL_FQDN} (router/Vault not ready?); skipping"
        )
        return True

    profiles = _firefox_profile_dirs(mc_base)
    if not profiles:
        log(f"vault_firefox_trust: no Firefox profiles with cert9.db under {mc_base}; skipping")
        return True

    ok = 0
    for p in profiles:
        if import_ca_pem_to_profile(ca_pem, p, log=log):
            ok += 1
            log(f"vault_firefox_trust: updated '{VAULT_CA_NICKNAME}' in {os.path.basename(p)}")

    if ok != len(profiles):
        log(f"vault_firefox_trust: imported to {ok}/{len(profiles)} profile(s)")
        return False

    log(f"vault_firefox_trust: Vault PKI root CA synced to {ok} Firefox profile(s)")
    return True

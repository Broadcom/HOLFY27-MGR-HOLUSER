#!/usr/bin/env python3
"""
Sync the active HashiCorp Vault PKI root CA into Firefox NSS profiles on the LMC.

Used by lab startup (prelim) so https://auth.vcf.lab, https://vault.vcf.lab, and
other Vault-signed endpoints trust after CA rotation. Mirrors the Vault step in
Tools/confighol-9.1.py without importing the full config script.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from typing import Callable, List, Optional

# NodePort PKI on holorouter (same as confighol-9.1.py)
VAULT_PK_CA_URL = "http://10.1.1.1:32000/v1/pki/ca/pem"
VAULT_CA_NICKNAME = "vcf.lab Root Authority"
CERTUTIL = "certutil"


def _firefox_profile_dirs(mc_base: str) -> List[str]:
    """Return profile paths under the console home tree (e.g. /lmchol/... on manager)."""
    base = os.path.join(
        mc_base, "home", "holuser", "snap", "firefox", "common", ".mozilla", "firefox"
    )
    if not os.path.isdir(base):
        return []
    out: List[str] = []
    for name in os.listdir(base):
        path = os.path.join(base, name)
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, "cert9.db")):
            out.append(path)
    return out


def download_vault_root_ca_pem(
    url: str = VAULT_PK_CA_URL, timeout: int = 15
) -> Optional[str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace").strip()
    except (urllib.error.URLError, OSError) as e:
        return None
    if body.startswith("-----BEGIN CERTIFICATE-----"):
        return body
    return None


def import_ca_pem_to_profile(
    ca_pem: str,
    profile_path: str,
    *,
    ca_name: str = VAULT_CA_NICKNAME,
    log: Optional[Callable[[str], None]] = None,
) -> bool:
    def _log(msg: str) -> None:
        if log:
            log(msg)

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".pem", delete=False, encoding="utf-8"
        ) as f:
            f.write(ca_pem)
            ca_file = f.name
    except OSError as e:
        _log(f"vault_firefox_trust: temp PEM failed: {e}")
        return False

    try:
        check_cmd = [CERTUTIL, "-L", "-d", f"sql:{profile_path}", "-n", ca_name]
        if subprocess.run(check_cmd, capture_output=True, text=True).returncode == 0:
            subprocess.run(
                [CERTUTIL, "-D", "-d", f"sql:{profile_path}", "-n", ca_name],
                capture_output=True,
            )
        # Drop stale auto-renamed duplicates (e.g. after CA rotation)
        for suffix in (" #2", " #3"):
            nick = f"{ca_name}{suffix}"
            subprocess.run(
                [CERTUTIL, "-D", "-d", f"sql:{profile_path}", "-n", nick],
                capture_output=True,
            )
        imp = subprocess.run(
            [
                CERTUTIL,
                "-A",
                "-d",
                f"sql:{profile_path}",
                "-n",
                ca_name,
                "-t",
                "CT,,",
                "-i",
                ca_file,
            ],
            capture_output=True,
            text=True,
        )
        if imp.returncode != 0:
            _log(f"vault_firefox_trust: certutil failed for {profile_path}: {imp.stderr}")
            return False
        return True
    finally:
        try:
            os.unlink(ca_file)
        except OSError:
            pass


def sync_vault_ca_to_firefox(
    mc_base: str,
    log: Callable[[str], None],
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
        log(
            "vault_firefox_trust: certutil not installed (apt install libnss3-tools); skipping"
        )
        return True

    ca_pem = download_vault_root_ca_pem()
    if not ca_pem:
        log(
            "vault_firefox_trust: could not download Vault CA from "
            f"{VAULT_PK_CA_URL} (router/Vault not ready?); skipping"
        )
        return True

    profiles = _firefox_profile_dirs(mc_base)
    if not profiles:
        log(
            f"vault_firefox_trust: no Firefox profiles with cert9.db under {mc_base}; skipping"
        )
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

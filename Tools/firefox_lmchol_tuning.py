#!/usr/bin/env python3
"""
Tune Firefox user.js on the Main Linux Console (LMC) profile.

Lab automation (prelim, manager scripts) edits the same home tree via ``/lmchol``
on the *manager* VM (NFS client mounting the console export). On the *console* VM,
Firefox reads ``~/snap/firefox/...`` from **local disk** — the browser process does
not use NFS for profile I/O there.

Wrong proxy settings (PAC type with a dead 10.0.0.1:3128) and heavy urlbar work
cause multi-minute startup stalls. This module rewrites a bounded block in each
profile's ``user.js``.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, List, Optional

BEGIN = "# --- BEGIN HOL LMC Firefox tuning ---"
END = "# --- END HOL LMC Firefox tuning ---"

# Lines we remove from the rest of user.js (will be re-applied inside HOL block).
_STRIP_LINE_RES = (
    re.compile(r'^\s*user_pref\s*\(\s*["\']network\.proxy\.'),
    re.compile(r'^\s*user_pref\s*\(\s*["\']browser\.urlbar\.quicksuggest'),
    re.compile(r'^\s*user_pref\s*\(\s*["\']browser\.places\.speculativeConnect'),
    re.compile(r'^\s*user_pref\s*\(\s*["\']network\.prefetch-next'),
    re.compile(r'^\s*user_pref\s*\(\s*["\']browser\.safebrowsing\.(malware|phish)\.enabled'),
    re.compile(r'^\s*user_pref\s*\(\s*["\']browser\.shell\.checkDefaultBrowser'),
    re.compile(r'^\s*user_pref\s*\(\s*["\']browser\.sessionstore\.resume_from_crash'),
)


def _firefox_profile_dirs(mc_base: str) -> List[str]:
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


def _user_js_path(profile_dir: str) -> str:
    return os.path.join(profile_dir, "user.js")


def _hol_block(proxy_host: str, proxy_port: int) -> str:
    # Manual proxy (type 1). PAC type 2 with http_* set is invalid and stalls startup.
    lines = [
        BEGIN,
        'user_pref("network.proxy.type", 1);',
        f'user_pref("network.proxy.http", "{proxy_host}");',
        f'user_pref("network.proxy.http_port", {proxy_port});',
        f'user_pref("network.proxy.ssl", "{proxy_host}");',
        f'user_pref("network.proxy.ssl_port", {proxy_port});',
        'user_pref("network.proxy.share_proxy_settings", true);',
        # Internal lab traffic should bypass Squid (DNS, vCenter, Vault NodePort, etc.)
        'user_pref("network.proxy.no_proxies_on", "localhost, 127.0.0.1, 10.1.1.1, '
        '192.168.0.2, *.vcf.lab, *.site-a.vcf.lab, *.site-b.vcf.lab");',
        # Urlbar / disk: reduce SQLite churn (large suggest.sqlite) and speculative work
        'user_pref("browser.urlbar.quicksuggest.enabled", false);',
        'user_pref("browser.urlbar.suggest.quicksuggest.sponsored", false);',
        'user_pref("browser.urlbar.suggest.quicksuggest.nonsponsored", false);',
        'user_pref("browser.places.speculativeConnect.enabled", false);',
        'user_pref("network.prefetch-next", false);',
        # Skip network-heavy checks during startup (lab is trusted)
        'user_pref("browser.safebrowsing.malware.enabled", false);',
        'user_pref("browser.safebrowsing.phish.enabled", false);',
        'user_pref("browser.shell.checkDefaultBrowser", false);',
        'user_pref("browser.sessionstore.resume_from_crash", false);',
        END,
        "",
    ]
    return "\n".join(lines)


def _rewrite_user_js(path: str, proxy_host: str, proxy_port: int) -> bool:
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw_lines = f.readlines()
    else:
        raw_lines = []

    out: List[str] = []
    skip = False
    for line in raw_lines:
        if BEGIN in line:
            skip = True
            continue
        if skip:
            if END in line:
                skip = False
            continue
        if any(r.search(line) for r in _STRIP_LINE_RES):
            continue
        out.append(line)
    # Trim trailing blank runs
    while out and out[-1].strip() == "":
        out.pop()
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    if out and out[-1].strip() != "":
        out.append("\n")

    block = _hol_block(proxy_host, proxy_port)
    out.append(block)
    text = "".join(out)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


def apply_firefox_lmchol_tuning(
    lsf: Any,
    dry_run: bool = False,
    proxy_host: Optional[str] = None,
    proxy_port: int = 3128,
) -> bool:
    """
    Update user.js in each LMC Firefox profile: correct manual Squid proxy and
    lightweight startup prefs (Quick Suggest off, etc.).

    :return: True if all profiles updated or none found; False on write error
    """
    log: Callable[[str], Any] = getattr(lsf, "write_output", print)
    mc = getattr(lsf, "mc", "/lmchol")
    host = proxy_host or getattr(lsf, "proxy", "proxy.site-a.vcf.lab")

    profiles = _firefox_profile_dirs(mc)
    if not profiles:
        log("firefox_lmchol_tuning: no Firefox profiles under LMC; skip")
        return True

    if dry_run:
        log(
            f"firefox_lmchol_tuning: dry-run — would tune {len(profiles)} profile(s) "
            f"(proxy {host}:{proxy_port})"
        )
        return True

    ok_all = True
    for prof in profiles:
        uj = _user_js_path(prof)
        try:
            _rewrite_user_js(uj, host, proxy_port)
            log(
                f"firefox_lmchol_tuning: updated user.js for profile {os.path.basename(prof)} "
                f"(manual proxy {host}:{proxy_port}, lightweight prefs)"
            )
        except OSError as e:
            log(f"WARNING: firefox_lmchol_tuning: could not write {uj}: {e}")
            ok_all = False
    return ok_all


def main() -> None:
    import argparse
    import lsfunctions as lsf

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--mc-base",
        default="/lmchol",
        help=(
            "Path to console home tree (/lmchol on manager = NFS export of "
            "console; same paths are local disk when running on the console)"
        ),
    )
    p.add_argument(
        "--proxy-host", default=None, help="Squid host (default from lsf.proxy)"
    )
    p.add_argument("--proxy-port", type=int, default=3128)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    lsf.init(router=False)
    # Minimal shim: only mc + proxy + write_output used
    class _Shim:
        mc = args.mc_base
        proxy = args.proxy_host or lsf.proxy
        write_output = staticmethod(print)

    apply_firefox_lmchol_tuning(_Shim(), dry_run=args.dry_run, proxy_port=args.proxy_port)


if __name__ == "__main__":
    main()

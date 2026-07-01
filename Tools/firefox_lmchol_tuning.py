#!/usr/bin/env python3
"""
Author: Burke Azbill and HOL Core Team
Version: 1.4 2026-06-30

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

import glob
import os
import re
import shutil
from typing import Any, Callable, List, Optional

BEGIN = "# --- BEGIN HOL LMC Firefox tuning ---"

# v1.3 Changes:
# - Added _resolve_ff_base() to support both apt and snap Firefox profile paths.
#   Apt Firefox stores profiles at ~/.mozilla/firefox/ (preferred); snap Firefox
#   stores them at ~/snap/firefox/common/.mozilla/firefox/. The helper tries the
#   apt path first and falls back to the snap path, so the module works before,
#   during, and after a snap→apt migration with no code changes required.
# v1.4 Changes:
# - Added crash reporter disable prefs to both HOL blocks (toolkit.crashreporter.enabled,
#   browser.tabs.crashReporting.sendReport, browser.crashReports.unsubmittedCheck.*).
#   In VM/VNC environments Firefox can hang on exit waiting for a crash reporter dialog
#   the user can never see or dismiss, causing "Firefox is already running" errors on
#   the next open attempt. Disabling the crash reporter breaks this deadlock.
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
    re.compile(r'^\s*user_pref\s*\(\s*["\']privacy\.clear(History|OnShutdown_v2|SiteData)\.'),
    re.compile(r'^\s*user_pref\s*\(\s*["\']toolkit\.crashreporter\.'),
    re.compile(r'^\s*user_pref\s*\(\s*["\']browser\.tabs\.crashReporting\.'),
    re.compile(r'^\s*user_pref\s*\(\s*["\']browser\.crashReports\.unsubmittedCheck\.'),
)


def _resolve_ff_base(mc_base: str) -> str:
    """Return the Firefox profiles base directory, preferring the apt path.

    Apt Firefox (deb package) stores profiles at ``~/.mozilla/firefox/``.
    Snap Firefox stores them at ``~/snap/firefox/common/.mozilla/firefox/``.
    We try the apt path first so the module works seamlessly after a snap→apt
    migration without requiring any configuration change.
    """
    apt_path  = os.path.join(mc_base, "home", "holuser", ".mozilla", "firefox")
    snap_path = os.path.join(
        mc_base, "home", "holuser", "snap", "firefox", "common", ".mozilla", "firefox"
    )
    return apt_path if os.path.isdir(apt_path) else snap_path


def _firefox_profile_dirs(mc_base: str) -> List[str]:
    base = _resolve_ff_base(mc_base)
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


def _hol_proxy_clear_block() -> str:
    """Return the user.js HOL block that disables the proxy (network.proxy.type=0).

    Used for non-HOL lab types (DISCOVERY, VXP, ATE, EDU) where no proxy filter
    is required.  Perf prefs (Quick Suggest, safe-browsing, etc.) are still
    applied so Firefox starts cleanly regardless of lab type.
    """
    lines = [
        BEGIN,
        'user_pref("network.proxy.type", 0);',
        # Urlbar / disk: reduce SQLite churn and speculative work
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
        # Disable crash reporter — prevents hang-on-exit and "already running" errors in VMs.
        # When Firefox exits uncleanly it launches crashreporter and waits for it; in a VM
        # the dialog is invisible and Firefox's parent process never releases the profile lock.
        'user_pref("toolkit.crashreporter.enabled", false);',
        'user_pref("browser.tabs.crashReporting.sendReport", false);',
        'user_pref("browser.crashReports.unsubmittedCheck.enabled", false);',
        'user_pref("browser.crashReports.unsubmittedCheck.autoSubmit2", false);',
        # Preserve saved usernames and form history across sessions
        'user_pref("privacy.clearHistory.formdata", false);',
        'user_pref("privacy.clearOnShutdown_v2.formdata", false);',
        'user_pref("privacy.clearSiteData.formdata", false);',
        'user_pref("privacy.clearSiteData.historyFormDataAndDownloads", false);',
        'user_pref("browser.formfill.enable", true);',
        'user_pref("browser.formfill.autoFill", true);',
        'user_pref("browser.formfill.autoFill.passwords", true);',
        'user_pref("browser.formfill.autoFill.forms", true);',
        'user_pref("signon.autofillForms", true);',
        'user_pref("signon.includeOtherSubdomainsInLookup", false)',
        'user_pref("messaging-system.rsexperimentloader.enabled", false);',
        END,
        "",
    ]
    return "\n".join(lines)


_FIREFOX_NO_PROXY_FALLBACK = (
    "localhost, 127.0.0.1, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 198.18.0.0/16,"
    " *.vcf.lab, *.svc, *.cluster.local"
)


def _build_firefox_no_proxy(lsf: Any) -> str:
    """Build the Firefox no_proxies_on string from lsf.LAB_NO_PROXY_PARTS.

    Converts dot-prefix entries (e.g. ``.vcf.lab``) to Firefox wildcard form
    (``*.vcf.lab``).  CIDR entries are passed through unchanged — Firefox Gecko
    supports CIDR notation in ``network.proxy.no_proxies_on`` since Firefox 88.
    Falls back to _FIREFOX_NO_PROXY_FALLBACK if the attribute is absent.
    """
    parts = getattr(lsf, "LAB_NO_PROXY_PARTS", None)
    if not parts:
        return _FIREFOX_NO_PROXY_FALLBACK
    converted = []
    for entry in parts:
        converted.append("*" + entry if entry.startswith(".") else entry)
    return ", ".join(converted)


def _hol_block(proxy_host: str, proxy_port: int, no_proxy: str = _FIREFOX_NO_PROXY_FALLBACK) -> str:
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
        f'user_pref("network.proxy.no_proxies_on", "{no_proxy}");',
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
        # Disable crash reporter — prevents hang-on-exit and "already running" errors in VMs.
        'user_pref("toolkit.crashreporter.enabled", false);',
        'user_pref("browser.tabs.crashReporting.sendReport", false);',
        'user_pref("browser.crashReports.unsubmittedCheck.enabled", false);',
        'user_pref("browser.crashReports.unsubmittedCheck.autoSubmit2", false);',
        # Preserve saved usernames and form history across sessions
        'user_pref("privacy.clearHistory.formdata", false);',
        'user_pref("privacy.clearOnShutdown_v2.formdata", false);',
        'user_pref("privacy.clearSiteData.formdata", false);',
        'user_pref("privacy.clearSiteData.historyFormDataAndDownloads", false);',
        'user_pref("browser.formfill.enable", true);',
        'user_pref("browser.formfill.autoFill", true);',
        'user_pref("browser.formfill.autoFill.passwords", true);',
        'user_pref("browser.formfill.autoFill.forms", true);',
        'user_pref("signon.autofillForms", true);',
        'user_pref("signon.includeOtherSubdomainsInLookup", false)',
        'user_pref("messaging-system.rsexperimentloader.enabled", false);',
        END,
        "",
    ]
    return "\n".join(lines)


def _rewrite_user_js(
    path: str,
    proxy_host: str,
    proxy_port: int,
    override_block: Optional[str] = None,
) -> bool:
    """Rewrite user.js, replacing the HOL marker block with a fresh one.

    :param override_block: If provided, insert this block instead of the
                           default ``_hol_block(proxy_host, proxy_port)`` block.
                           Pass the output of ``_hol_proxy_clear_block()`` to
                           disable the proxy for non-HOL lab types.
    """
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

    block = override_block if override_block is not None else _hol_block(proxy_host, proxy_port)
    out.append(block)
    text = "".join(out)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


def _clear_crashes_and_idb(
    mc_base: str, profiles: List[str], log: Callable[[str], Any], dry_run: bool
) -> None:
    # 1. Clear Crash Reports
    base = _resolve_ff_base(mc_base)
    for subdir in ["pending", "submitted"]:
        target_dir = os.path.join(base, "Crash Reports", subdir)
        if os.path.isdir(target_dir):
            if dry_run:
                log(f"firefox_lmchol_tuning: dry-run — would clear {target_dir}")
            else:
                for item in os.listdir(target_dir):
                    item_path = os.path.join(target_dir, item)
                    try:
                        if os.path.isdir(item_path):
                            shutil.rmtree(item_path)
                        else:
                            os.remove(item_path)
                    except OSError as e:
                        log(f"WARNING: firefox_lmchol_tuning: could not remove {item_path}: {e}")
                log(f"firefox_lmchol_tuning: cleared crash reports in {target_dir}")

    # 2. Clear remote-settings IDB in each profile
    for prof in profiles:
        idb_dir = os.path.join(prof, "storage", "permanent", "chrome", "idb")
        if os.path.isdir(idb_dir):
            pattern = os.path.join(idb_dir, "*rsegmnoittet-es.*")
            matches = glob.glob(pattern)
            if matches:
                if dry_run:
                    log(
                        f"firefox_lmchol_tuning: dry-run — would clear {len(matches)} "
                        f"remote-settings idb files in {os.path.basename(prof)}"
                    )
                else:
                    for f in matches:
                        try:
                            if os.path.isdir(f):
                                shutil.rmtree(f)
                            else:
                                os.remove(f)
                        except OSError as e:
                            log(f"WARNING: firefox_lmchol_tuning: could not remove {f}: {e}")
                    log(f"firefox_lmchol_tuning: cleared remote-settings idb files in {os.path.basename(prof)}")


def apply_firefox_lmchol_tuning(
    lsf: Any,
    dry_run: bool = False,
    clear: bool = False,
    proxy_host: Optional[str] = None,
    proxy_port: int = 3128,
) -> bool:
    """Update user.js in each LMC Firefox profile.

    When ``clear=False`` (default / HOL lab types): writes the manual Squid
    proxy block plus lightweight startup prefs.

    When ``clear=True`` (non-HOL lab types — DISCOVERY, VXP, ATE, EDU): writes
    ``network.proxy.type=0`` (no proxy) plus the same startup perf prefs.

    :param lsf:        lsfunctions module reference (or any object with
                       ``write_output``, ``mc``, and ``proxy`` attributes).
    :param dry_run:    If True log intent but make no changes.
    :param clear:      If True write the proxy-clear block instead of the
                       proxy-set block.
    :param proxy_host: Squid host (default from lsf.proxy).
    :param proxy_port: Squid port (default 3128).
    :return: True if all profiles updated or none found; False on write error.
    """
    log: Callable[[str], Any] = getattr(lsf, "write_output", print)
    mc = getattr(lsf, "mc", "/lmchol")
    host = proxy_host or getattr(lsf, "proxy", "proxy.site-a.vcf.lab")
    no_proxy = _build_firefox_no_proxy(lsf)
    mode_desc = "clear (no proxy)" if clear else f"set (manual proxy {host}:{proxy_port})"

    profiles = _firefox_profile_dirs(mc)
    if not profiles:
        log("firefox_lmchol_tuning: no Firefox profiles under LMC; skip")
        return True

    if dry_run:
        log(
            f"firefox_lmchol_tuning: dry-run — would tune {len(profiles)} profile(s) "
            f"({mode_desc})"
        )
        _clear_crashes_and_idb(mc, profiles, log, dry_run=True)
        return True

    _clear_crashes_and_idb(mc, profiles, log, dry_run=False)

    ok_all = True
    for prof in profiles:
        uj = _user_js_path(prof)
        try:
            if clear:
                _rewrite_user_js(uj, host, proxy_port, override_block=_hol_proxy_clear_block())
                log(
                    f"firefox_lmchol_tuning: user.js {os.path.basename(prof)} "
                    f"— proxy cleared (type=0)"
                )
            else:
                _rewrite_user_js(
                    uj, host, proxy_port,
                    override_block=_hol_block(host, proxy_port, no_proxy),
                )
                log(
                    f"firefox_lmchol_tuning: user.js {os.path.basename(prof)} "
                    f"— manual proxy {host}:{proxy_port}"
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
    # Minimal shim: forwards the attributes consumed by apply_firefox_lmchol_tuning
    # and _build_firefox_no_proxy so the canonical lsf values are always used.
    class _Shim:
        mc = args.mc_base
        proxy = args.proxy_host or lsf.proxy
        LAB_NO_PROXY_PARTS = lsf.LAB_NO_PROXY_PARTS
        write_output = staticmethod(print)

    apply_firefox_lmchol_tuning(_Shim(), dry_run=args.dry_run, proxy_port=args.proxy_port)


if __name__ == "__main__":
    main()

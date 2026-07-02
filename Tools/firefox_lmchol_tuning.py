#!/usr/bin/env python3
"""
Author: Burke Azbill and HOL Core Team
Version: 1.6 2026-07-02

Configure Firefox on the Main Linux Console (LMC) via enterprise policies.

Lab automation (prelim, manager scripts) runs this module via ``/lmchol``
on the *manager* VM (NFS client mounting the console root export).  On the
*console* VM, Firefox reads ``~/snap/firefox/...`` and ``/etc/firefox/``
from **local disk**.

This module writes two system-level files that apply to every Firefox profile
and survive profile resets:

  /etc/firefox/policies/policies.json
      Enterprise policy file read by snap Firefox via the ``etc-firefox``
      system-files interface.  Covers proxy, Nimbus/experiments, telemetry,
      form-data preservation, and perf/startup settings.

  /etc/environment
      Adds ``MOZ_CRASHREPORTER_DISABLE=1`` to suppress the crash reporter at
      launch (before any profile prefs are loaded, which is when the snap
      crashreporter process is spawned).  A console re-login or reboot is
      required for this to take effect on an already-running session.

Any legacy ``user.js`` HOL block written by earlier versions of this module is
purged from each profile so it cannot shadow or conflict with the policies.

Version history
---------------
v1.3  Added _resolve_ff_base() for apt/snap Firefox path detection.
v1.4  Added crash reporter disable prefs to HOL blocks.
v1.5  Added _write_firefox_policies() for UserMessaging.FirefoxLabs=false
      policy, fixing the RemoteSettingsExperimentLoader shutdown crash.
v1.6  Full migration from user.js prefs to enterprise policies.  Removes all
      user.js writing code.  The policies.json now carries the complete tuning
      set.  The crash reporter is handled via MOZ_CRASHREPORTER_DISABLE=1 in
      /etc/environment instead of an ineffective pref.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
from typing import Any, Callable, Dict, List, Optional

# Sentinel strings used to locate the legacy HOL block in user.js files so it
# can be stripped during migration.  Not written by this version.
_LEGACY_BEGIN = "// --- BEGIN HOL LMC Firefox tuning ---"
_LEGACY_END   = "// --- END HOL LMC Firefox tuning ---"

# Patterns matching individual prefs that were managed outside the HOL block in
# older profiles.  Stripped alongside the block during user.js purge.
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

_FIREFOX_NO_PROXY_FALLBACK = (
    "localhost, 127.0.0.1, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 198.18.0.0/16,"
    " *.vcf.lab, *.svc, *.cluster.local"
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


def _build_policies(
    clear: bool,
    proxy_host: str,
    proxy_port: int,
    no_proxy: str,
) -> Dict[str, Any]:
    """Return the policies.json content dict for this lab variant.

    :param clear:      When True, set proxy Mode=none (non-HOL lab types).
                       When False, set Mode=manual with Squid settings (HOL).
    :param proxy_host: Squid hostname, used only when clear=False.
    :param proxy_port: Squid port, used only when clear=False.
    :param no_proxy:   Comma-separated bypass list for the Proxy Passthrough
                       field, used only when clear=False.
    """
    if clear:
        proxy: Dict[str, Any] = {"Mode": "none", "Locked": True}
    else:
        proxy = {
            "Mode": "manual",
            "HTTPProxy": f"{proxy_host}:{proxy_port}",
            "SSLProxy": f"{proxy_host}:{proxy_port}",
            "UseHTTPProxyForAllProtocols": True,
            "Passthrough": no_proxy,
            "Locked": True,
        }

    return {
        "policies": {
            # Proxy — manual Squid or direct depending on lab type.
            "Proxy": proxy,

            # Telemetry — superset of datareporting.healthreport.uploadEnabled;
            # also disables dataSubmission and telemetry archive.
            "DisableTelemetry": True,

            # Nimbus/experiments — three policies work together to force
            # ExperimentAPI.enabled=False in Firefox 151:
            #
            #   DisableFirefoxStudies  → disallows "Shield"
            #                           → studiesEnabled=False
            #   DisableRemoteImprovements → disallows "NimbusRollouts"
            #                           → rolloutsEnabled=False
            #   UserMessaging.FirefoxLabs → disallows "FirefoxLabs"
            #                           → labsEnabled=False
            #
            # With all three False, ExperimentAPI.enabled returns False,
            # RemoteSettingsExperimentLoader.enable() returns early, and the
            # async shutdown barrier that caused the SIGSEGV-on-close crash is
            # never registered.  Prefs alone cannot achieve this because
            # labsEnabled is Services.policies.isAllowed("FirefoxLabs") which
            # ignores user prefs.
            "DisableFirefoxStudies": True,
            "DisableRemoteImprovements": True,
            "UserMessaging": {"FirefoxLabs": False},

            # Default browser prompt — lab machines are always the default.
            "DontCheckDefaultBrowser": True,

            # Form data preservation — boolean False locks
            # privacy.sanitize.sanitizeOnShutdown=false and every
            # clearOnShutdown category to false, preserving saved usernames and
            # form history across sessions.
            "SanitizeOnShutdown": False,

            # Generic pref overrides — all within the Preferences policy
            # allowlist (browser.*, network.*, signon.*, places.*).
            # Flat form (non-object value) = set and lock.
            "Preferences": {
                # Urlbar/disk: reduce SQLite churn and speculative network work.
                "browser.urlbar.quicksuggest.enabled": False,
                "browser.urlbar.suggest.quicksuggest.sponsored": False,
                "browser.urlbar.suggest.quicksuggest.nonsponsored": False,
                "browser.places.speculativeConnect.enabled": False,
                "network.prefetch-next": False,
                # Skip network-heavy checks on startup (lab is trusted).
                "browser.safebrowsing.malware.enabled": False,
                "browser.safebrowsing.phish.enabled": False,
                # Crash reporter UI prefs (belt-and-suspenders alongside the
                # MOZ_CRASHREPORTER_DISABLE env var).
                "browser.sessionstore.resume_from_crash": False,
                "browser.tabs.crashReporting.sendReport": False,
                "browser.crashReports.unsubmittedCheck.enabled": False,
                "browser.crashReports.unsubmittedCheck.autoSubmit2": False,
                # Autofill — preserve form and credential autofill behaviour.
                "browser.formfill.enable": True,
                "browser.formfill.autoFill": True,
                "browser.formfill.autoFill.passwords": True,
                "browser.formfill.autoFill.forms": True,
                "signon.autofillForms": True,
                "signon.includeOtherSubdomainsInLookup": False,
            },
        }
    }


def _write_firefox_policies(
    mc_base: str,
    policies: Dict[str, Any],
    log: Callable[[str], Any],
    dry_run: bool,
) -> None:
    """Create or update /etc/firefox/policies/policies.json via mc_base.

    When run from the manager VM, mc_base is ``/lmchol`` (NFS mount of the
    console root), so the effective path on the console is
    ``/etc/firefox/policies/policies.json``.

    Snap Firefox has the ``etc-firefox`` system-files interface connected,
    which means it reads enterprise policies from ``/etc/firefox/policies/``.
    The write is idempotent — the file is only updated when the content changes.
    """
    policies_dir  = os.path.join(mc_base, "etc", "firefox", "policies")
    policies_path = os.path.join(policies_dir, "policies.json")
    desired       = json.dumps(policies, indent=2) + "\n"

    if dry_run:
        log(f"firefox_lmchol_tuning: dry-run — would write {policies_path}")
        return

    try:
        os.makedirs(policies_dir, exist_ok=True)
    except OSError as e:
        log(f"WARNING: firefox_lmchol_tuning: cannot create {policies_dir}: {e}")
        return

    existing: Optional[str] = None
    if os.path.isfile(policies_path):
        try:
            with open(policies_path, "r", encoding="utf-8") as fh:
                existing = fh.read()
        except OSError:
            pass

    if existing == desired:
        log(f"firefox_lmchol_tuning: {policies_path} already up-to-date")
        return

    try:
        with open(policies_path, "w", encoding="utf-8") as fh:
            fh.write(desired)
        log(f"firefox_lmchol_tuning: wrote {policies_path}")
    except OSError as e:
        log(f"WARNING: firefox_lmchol_tuning: cannot write {policies_path}: {e}")


def _write_crashreporter_env(
    mc_base: str, log: Callable[[str], Any], dry_run: bool
) -> None:
    """Ensure MOZ_CRASHREPORTER_DISABLE=1 is present in /etc/environment.

    The toolkit.crashreporter.enabled pref is ineffective in snap Firefox
    because the crash reporter process is spawned before profile prefs are
    read.  Setting this environment variable suppresses it at the OS level.

    The write is idempotent — the line is only appended when absent.
    A console re-login or reboot is required for a running session to pick
    up the change.
    """
    env_path = os.path.join(mc_base, "etc", "environment")
    marker   = "MOZ_CRASHREPORTER_DISABLE"
    new_line = "MOZ_CRASHREPORTER_DISABLE=1\n"

    if dry_run:
        log(f"firefox_lmchol_tuning: dry-run — would ensure {marker} in {env_path}")
        return

    existing = ""
    if os.path.isfile(env_path):
        try:
            with open(env_path, "r", encoding="utf-8", errors="replace") as fh:
                existing = fh.read()
        except OSError as e:
            log(f"WARNING: firefox_lmchol_tuning: cannot read {env_path}: {e}")
            return

    if marker in existing:
        log(f"firefox_lmchol_tuning: {marker} already present in {env_path}")
        return

    try:
        with open(env_path, "a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(new_line)
        log(f"firefox_lmchol_tuning: added {marker} to {env_path}")
    except OSError as e:
        log(f"WARNING: firefox_lmchol_tuning: cannot write {env_path}: {e}")


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


def _purge_user_js_block(
    path: str, log: Callable[[str], Any], dry_run: bool
) -> None:
    """Remove the legacy HOL BEGIN/END pref block from user.js.

    With all tuning migrated to enterprise policies, user.js no longer needs
    any HOL content.  This purge prevents stale prefs from shadowing or
    conflicting with policy-set values.

    Also strips any standalone HOL-managed prefs that may exist outside the
    marker block in very old profiles (covered by _STRIP_LINE_RES).

    Prefs the user added independently (e.g. ``general.useragent.override``)
    are left untouched.
    """
    if not os.path.isfile(path):
        return

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw_lines = fh.readlines()

    out: List[str] = []
    skip    = False
    changed = False
    for line in raw_lines:
        if _LEGACY_BEGIN in line:
            skip    = True
            changed = True
            continue
        if skip:
            if _LEGACY_END in line:
                skip = False
            continue
        if any(r.search(line) for r in _STRIP_LINE_RES):
            changed = True
            continue
        out.append(line)

    profile_name = os.path.basename(os.path.dirname(path))

    if not changed:
        log(
            f"firefox_lmchol_tuning: user.js {profile_name} "
            f"— no legacy HOL block found, nothing to purge"
        )
        return

    if dry_run:
        log(f"firefox_lmchol_tuning: dry-run — would purge HOL block from {path}")
        return

    # Trim trailing blank lines then write back.
    while out and out[-1].strip() == "":
        out.pop()

    text = "".join(out)
    if text and not text.endswith("\n"):
        text += "\n"

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    log(f"firefox_lmchol_tuning: purged HOL block from user.js {profile_name}")


def apply_firefox_lmchol_tuning(
    lsf: Any,
    dry_run: bool = False,
    clear: bool = False,
    proxy_host: Optional[str] = None,
    proxy_port: int = 3128,
) -> bool:
    """Apply Firefox LMC tuning via enterprise policies and environment variable.

    Writes ``/etc/firefox/policies/policies.json`` (via mc_base) with the full
    policy set covering proxy, Nimbus/experiments, telemetry, form-data
    preservation, and perf/startup settings.  Appends
    ``MOZ_CRASHREPORTER_DISABLE=1`` to ``/etc/environment``.  Strips the legacy
    user.js HOL block from each profile.

    When ``clear=False`` (default / HOL lab types): the Proxy policy uses
    Mode=manual pointing at proxy_host:proxy_port.

    When ``clear=True`` (non-HOL lab types — DISCOVERY, VXP, ATE, EDU): the
    Proxy policy uses Mode=none (direct connection).

    :param lsf:        lsfunctions module reference (or any object with
                       ``write_output``, ``mc``, and ``proxy`` attributes).
    :param dry_run:    If True log intent but make no changes.
    :param clear:      If True write the no-proxy policy variant.
    :param proxy_host: Squid host (default from lsf.proxy).
    :param proxy_port: Squid port (default 3128).
    :return: True if all operations succeeded or no profiles found; False on
             any write error.
    """
    log:  Callable[[str], Any] = getattr(lsf, "write_output", print)
    mc         = getattr(lsf, "mc", "/lmchol")
    host       = proxy_host or getattr(lsf, "proxy", "proxy.site-a.vcf.lab")
    no_proxy   = _build_firefox_no_proxy(lsf)
    mode_desc  = "clear (no proxy)" if clear else f"set (manual proxy {host}:{proxy_port})"

    profiles = _firefox_profile_dirs(mc)
    if not profiles:
        log("firefox_lmchol_tuning: no Firefox profiles under LMC; skip")
        return True

    policies = _build_policies(clear, host, proxy_port, no_proxy)

    if dry_run:
        log(
            f"firefox_lmchol_tuning: dry-run — would tune {len(profiles)} profile(s) "
            f"({mode_desc})"
        )
        _clear_crashes_and_idb(mc, profiles, log, dry_run=True)
        _write_firefox_policies(mc, policies, log, dry_run=True)
        _write_crashreporter_env(mc, log, dry_run=True)
        for prof in profiles:
            _purge_user_js_block(_user_js_path(prof), log, dry_run=True)
        return True

    ok_all = True
    _clear_crashes_and_idb(mc, profiles, log, dry_run=False)
    _write_firefox_policies(mc, policies, log, dry_run=False)
    _write_crashreporter_env(mc, log, dry_run=False)
    for prof in profiles:
        try:
            _purge_user_js_block(_user_js_path(prof), log, dry_run=False)
        except OSError as e:
            log(f"WARNING: firefox_lmchol_tuning: could not purge {_user_js_path(prof)}: {e}")
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
            "Path to console root tree (/lmchol on manager = NFS export of "
            "console; same paths are local disk when running on the console)"
        ),
    )
    p.add_argument(
        "--proxy-host", default=None, help="Squid host (default from lsf.proxy)"
    )
    p.add_argument("--proxy-port", type=int, default=3128)
    p.add_argument(
        "--clear",
        action="store_true",
        help="Write no-proxy policy variant (non-HOL lab types)",
    )
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

    apply_firefox_lmchol_tuning(
        _Shim(),
        dry_run=args.dry_run,
        clear=args.clear,
        proxy_port=args.proxy_port,
    )


if __name__ == "__main__":
    main()

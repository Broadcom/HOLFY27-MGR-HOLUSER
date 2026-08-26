#!/usr/bin/env python3
"""
Author: Burke Azbill and HOL Core Team
Version: 1.9 2026-08-26

Configure Firefox on the Main Linux Console (LMC) via enterprise policies and profile tuning.

Lab automation (prelim, manager scripts) runs this module on the *manager*
VM.  Firefox profile discovery/cleanup and SQLite tuning use ``/lmchol`` (NFS client
mounting the console root export) since those paths are ``holuser``-owned
and writable over NFS.  The two system-level files below are root-owned on
the console, so they are written over SSH as root instead:

  /etc/firefox/policies/policies.json
      Enterprise policy file read by snap Firefox via the ``etc-firefox``
      system-files interface.  Covers proxy, Nimbus/experiments, telemetry,
      form-data preservation, and perf/startup settings.

  /etc/environment
      Adds ``MOZ_CRASHREPORTER_DISABLE=1`` to suppress the crash reporter at
      launch (before any profile prefs are loaded, which is when the snap
      crashreporter process is spawned).  A console re-login or reboot is
      required for this to take effect on an already-running session.

  content-prefs.sqlite (per profile)
      Configures the global default page zoom level (default 80% / 0.8) so
      that newly started browser sessions and tabs open at 80% zoom.
      Purges any stale site-specific zoom overrides so all sites inherit
      the 80% default zoom while still allowing users to adjust zoom levels
      as desired during their active lab session.

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
v1.7  Fixed policies.json/environment never being written: both files are
      root-owned on the console, but the write went through the /lmchol NFS
      mount as the unprivileged holuser, which always failed with EACCES
      (logged as a WARNING and silently ignored).  Both writes now go over
      SSH as root via lsf.set_console_firefox_policies() /
      lsf.set_console_crashreporter_env(), mirroring the existing
      set_console_os_proxy() pattern used for the same /etc/environment file
      elsewhere in prelim.py.  A write failure now also fails the overall
      call so it can no longer be silently swallowed.
v1.8  Stopped hardcoding the proxy host/port.  The Firefox Proxy policy now
      defaults to lsf.LAB_PROXY_IP / lsf.LAB_PROXY_PORT — the canonical proxy
      constants lsfunctions.py already uses for every other proxy consumer —
      instead of the local "proxy.site-a.vcf.lab" / 3128 literals.  This
      also fixes the proxy_host/proxy_port parameters being effectively
      unusable from prelim.py (which never passed them, so the hardcoded
      defaults always won regardless of lsfunctions.py's actual config).
v1.9  Configured default page zoom level (80% / 0.8) in content-prefs.sqlite
      across all Firefox profiles, ensuring new tabs and sessions start at 80%
      zoom while allowing user adjustments during active lab usage.  Updated
      user.js purge regex to match both '//' and '#' marker comments. Added
      styled help screen conforming to script-help-style standard.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import sqlite3
import sys
import time
from typing import Any, Callable, Dict, List, Optional

VERSION = "1.9"
DATE = "2026-08-26"

# ANSI color codes for styled terminal output
if sys.stdout.isatty():
    _CYAN, _BLUE, _GREEN, _BOLD, _YELLOW, _RED, _NC = (
        "\033[0;36m",
        "\033[38;2;0;176;255m",
        "\033[0;32m",
        "\033[1m",
        "\033[1;33m",
        "\033[0;31m",
        "\033[0m",
    )
else:
    _CYAN = _BLUE = _GREEN = _BOLD = _YELLOW = _RED = _NC = ""

# Sentinel patterns used to locate the legacy HOL block in user.js files so it
# can be stripped during migration. Matches both '//' and '#' marker prefixes.
_LEGACY_BEGIN_RE = re.compile(r"^\s*(//|#)\s*---\s*BEGIN HOL LMC Firefox tuning\s*---")
_LEGACY_END_RE   = re.compile(r"^\s*(//|#)\s*---\s*END HOL LMC Firefox tuning\s*---")

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
                # Prevent site specific zoom differences across tabs
                "browser.zoom.siteSpecific": False,
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


def _set_default_zoom(
    profile_dir: str,
    zoom_level: float = 0.8,
    log: Callable[[str], Any] = print,
    dry_run: bool = False,
) -> bool:
    """Set the global default zoom level in content-prefs.sqlite.

    Firefox stores the global default page zoom level as a row in
    ``content-prefs.sqlite`` where settingID corresponds to
    ``browser.content.full-zoom`` and groupID is NULL (indicating global scope).
    Site-specific zoom values have non-NULL groupIDs referencing the ``groups``
    table.

    This function:
    1. Ensures the schema exists (tables: groups, settings, prefs, and indexes).
    2. Ensures ``browser.content.full-zoom`` exists in the ``settings`` table.
    3. Sets or updates the global pref (``groupID IS NULL``) to ``zoom_level`` (default 0.8 = 80%).
    4. Deletes any stale site-specific zoom overrides (``groupID IS NOT NULL``) so
       all sites start cleanly at the desired 80% default zoom while still
       allowing user zoom adjustments during their active lab session.
    """
    db_path = os.path.join(profile_dir, "content-prefs.sqlite")
    prof_name = os.path.basename(profile_dir)
    zoom_pct = int(round(zoom_level * 100))

    if dry_run:
        log(
            f"firefox_lmchol_tuning: dry-run — would set default zoom to {zoom_pct}% "
            f"in {prof_name}/content-prefs.sqlite"
        )
        return True

    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        cur = conn.cursor()

        # Ensure base tables exist
        cur.execute(
            "CREATE TABLE IF NOT EXISTS groups ("
            "id INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL"
            ")"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS settings ("
            "id INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL"
            ")"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS prefs ("
            "id INTEGER PRIMARY KEY, "
            "groupID INTEGER REFERENCES groups(id), "
            "settingID INTEGER NOT NULL REFERENCES settings(id), "
            "value BLOB, "
            "timestamp INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS groups_idx ON groups(name)")
        cur.execute("CREATE INDEX IF NOT EXISTS settings_idx ON settings(name)")
        cur.execute("CREATE INDEX IF NOT EXISTS prefs_idx ON prefs(timestamp, groupID, settingID)")

        # Ensure setting entry for browser.content.full-zoom
        cur.execute("SELECT id FROM settings WHERE name = 'browser.content.full-zoom'")
        row = cur.fetchone()
        if row:
            setting_id = row[0]
        else:
            cur.execute("INSERT INTO settings (name) VALUES ('browser.content.full-zoom')")
            setting_id = cur.lastrowid

        now = int(time.time())

        # Set or update global default zoom (groupID IS NULL)
        cur.execute(
            "SELECT id FROM prefs WHERE groupID IS NULL AND settingID = ?",
            (setting_id,),
        )
        global_row = cur.fetchone()
        if global_row:
            cur.execute(
                "UPDATE prefs SET value = ?, timestamp = ? WHERE id = ?",
                (zoom_level, now, global_row[0]),
            )
        else:
            cur.execute(
                "INSERT INTO prefs (groupID, settingID, value, timestamp) VALUES (NULL, ?, ?, ?)",
                (setting_id, zoom_level, now),
            )

        # Clear stale site-specific zoom overrides so all sites inherit the 80% default
        cur.execute(
            "DELETE FROM prefs WHERE settingID = ? AND groupID IS NOT NULL",
            (setting_id,),
        )

        conn.commit()
        conn.close()
        log(
            f"firefox_lmchol_tuning: set default zoom to {zoom_pct}% "
            f"in {prof_name}/content-prefs.sqlite"
        )
        return True
    except Exception as e:
        log(f"WARNING: firefox_lmchol_tuning: could not set zoom in {db_path}: {e}")
        return False


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
        if _LEGACY_BEGIN_RE.search(line):
            skip    = True
            changed = True
            continue
        if skip:
            if _LEGACY_END_RE.search(line):
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
    proxy_port: Optional[int] = None,
    console_host: str = "root@console.site-a.vcf.lab",
    zoom_level: float = 0.8,
) -> bool:
    """Apply Firefox LMC tuning via enterprise policies, environment variable, and profile DB.

    Writes ``/etc/firefox/policies/policies.json`` with the full policy set
    covering proxy, Nimbus/experiments, telemetry, form-data preservation,
    and perf/startup settings.  Appends ``MOZ_CRASHREPORTER_DISABLE=1`` to
    ``/etc/environment``.  Sets the default zoom level to 80% (0.8) in
    ``content-prefs.sqlite`` and strips legacy user.js HOL blocks from each profile.

    Both system files are root-owned on the console, so they are written
    over SSH as root (``lsf.set_console_firefox_policies`` /
    ``lsf.set_console_crashreporter_env``) rather than through the ``/lmchol``
    NFS mount, which is only writable as the unprivileged holuser.  Profile
    discovery, zoom configuration, and the user.js purge use ``/lmchol`` since
    those paths are holuser-owned.

    When ``clear=False`` (default / HOL lab types): the Proxy policy uses
    Mode=manual pointing at proxy_host:proxy_port, sourced from lsfunctions.py's
    canonical LAB_PROXY_IP / LAB_PROXY_PORT constants unless overridden.

    When ``clear=True`` (non-HOL lab types — DISCOVERY, VXP, ATE, EDU): the
    Proxy policy uses Mode=none (direct connection).

    :param lsf:          lsfunctions module reference (or any object with
                         ``write_output``, ``mc``, ``LAB_PROXY_IP``,
                         ``LAB_PROXY_PORT``, ``LAB_NO_PROXY_PARTS``, and
                         ``get_password`` attributes).
    :param dry_run:      If True log intent but make no changes.
    :param clear:        If True write the no-proxy policy variant.
    :param proxy_host:   Proxy host override (default lsf.LAB_PROXY_IP).
    :param proxy_port:   Proxy port override (default lsf.LAB_PROXY_PORT).
    :param console_host: SSH target for the root-owned file writes, e.g.
                         'root@console.site-a.vcf.lab'.
    :param zoom_level:   Default page zoom level (default 0.8 for 80%).
    :return: True if all operations succeeded or no profiles found; False on
             any write error.
    """
    log:  Callable[[str], Any] = getattr(lsf, "write_output", print)
    mc         = getattr(lsf, "mc", "/lmchol")
    host       = proxy_host or getattr(lsf, "LAB_PROXY_IP", "10.1.1.1")
    port       = proxy_port or getattr(lsf, "LAB_PROXY_PORT", 3128)
    no_proxy   = _build_firefox_no_proxy(lsf)
    password   = lsf.get_password()
    mode_desc  = "clear (no proxy)" if clear else f"set (manual proxy {host}:{port})"
    zoom_pct   = int(round(zoom_level * 100))

    profiles = _firefox_profile_dirs(mc)
    if not profiles:
        log("firefox_lmchol_tuning: no Firefox profiles under LMC; skip")
        return True

    policies = _build_policies(clear, host, port, no_proxy)

    if dry_run:
        log(
            f"firefox_lmchol_tuning: dry-run — would tune {len(profiles)} profile(s) "
            f"({mode_desc}, zoom={zoom_pct}%)"
        )
        _clear_crashes_and_idb(mc, profiles, log, dry_run=True)
        lsf.set_console_firefox_policies(console_host, password, policies, dry_run=True)
        lsf.set_console_crashreporter_env(console_host, password, dry_run=True)
        for prof in profiles:
            _purge_user_js_block(_user_js_path(prof), log, dry_run=True)
            _set_default_zoom(prof, zoom_level=zoom_level, log=log, dry_run=True)
        return True

    ok_all = True
    _clear_crashes_and_idb(mc, profiles, log, dry_run=False)
    if not lsf.set_console_firefox_policies(console_host, password, policies, dry_run=False):
        ok_all = False
    if not lsf.set_console_crashreporter_env(console_host, password, dry_run=False):
        ok_all = False
    for prof in profiles:
        try:
            _purge_user_js_block(_user_js_path(prof), log, dry_run=False)
        except OSError as e:
            log(f"WARNING: firefox_lmchol_tuning: could not purge {_user_js_path(prof)}: {e}")
            ok_all = False
        try:
            if not _set_default_zoom(prof, zoom_level=zoom_level, log=log, dry_run=False):
                ok_all = False
        except Exception as e:
            log(f"WARNING: firefox_lmchol_tuning: could not set zoom in {prof}: {e}")
            ok_all = False
    return ok_all


def show_help() -> None:
    """Display styled help screen conforming to script-help-style standard."""
    W = 70
    title = "Firefox LMC Console Tuning"
    print(f"\n{_CYAN}╔{'═' * W}╗{_NC}")
    print(f"{_CYAN}║{_NC}{_BLUE}{title:^{W}}{_NC}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{f'Version {VERSION}  —  {DATE}':^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}╚{'═' * W}╝{_NC}\n")
    print(f"{_BOLD}USAGE:{_NC}\n    firefox_lmchol_tuning.py [OPTIONS]\n")
    print(f"{_BOLD}OPTIONS:{_NC}")
    print(f"    {_GREEN}--mc-base{_NC} <path>          Path to console root mount (default: /lmchol)")
    print(f"    {_GREEN}--zoom{_NC} <level>            Initial default zoom level (default: 0.8 / 80%)")
    print(f"    {_GREEN}--proxy-host{_NC} <host>       Proxy host override (default from lsf.LAB_PROXY_IP)")
    print(f"    {_GREEN}--proxy-port{_NC} <port>       Proxy port override (default from lsf.LAB_PROXY_PORT)")
    print(f"    {_GREEN}--clear{_NC}                    Write no-proxy policy variant (non-HOL lab types)")
    print(f"    {_GREEN}--console-host{_NC} <host>     SSH target for root writes (default: root@console.site-a.vcf.lab)")
    print(f"    {_GREEN}--dry-run{_NC}                 Log intent without making changes")
    print(f"    {_GREEN}-h, --help{_NC}                Show this help message\n")
    print(f"{_YELLOW}EXAMPLES:{_NC}")
    print(f"    {_GREEN}# Apply standard LMC tuning (80% default zoom + enterprise policies){_NC}")
    print("    python3 Tools/firefox_lmchol_tuning.py --mc-base /lmchol\n")
    print(f"    {_GREEN}# Dry run check without applying changes{_NC}")
    print("    python3 Tools/firefox_lmchol_tuning.py --dry-run\n")
    print(f"    {_GREEN}# Apply tuning with a custom zoom level (e.g. 90%){_NC}")
    print("    python3 Tools/firefox_lmchol_tuning.py --zoom 0.9\n")
    print(f"    {_GREEN}# Clear proxy policy for non-HOL lab types{_NC}")
    print("    python3 Tools/firefox_lmchol_tuning.py --clear\n")
    sys.exit(0)


class _HelpOnErrorParser(argparse.ArgumentParser):
    """Custom parser to format errors and display styled help on parse failures."""
    def error(self, message: str) -> None:
        print(f"{_RED}ERROR:{_NC} {message}\n", file=sys.stderr)
        show_help()


def main() -> None:
    if "--help" in sys.argv or "-h" in sys.argv:
        show_help()

    import lsfunctions as lsf

    p = _HelpOnErrorParser(description=__doc__, add_help=False)
    p.add_argument(
        "--mc-base",
        default="/lmchol",
        help=(
            "Path to console root tree (/lmchol on manager = NFS export of "
            "console; same paths are local disk when running on the console)"
        ),
    )
    p.add_argument(
        "--zoom",
        type=float,
        default=0.8,
        help="Initial default page zoom level (default 0.8 for 80%%)",
    )
    p.add_argument(
        "--proxy-host", default=None, help="Proxy host override (default from lsf.LAB_PROXY_IP)"
    )
    p.add_argument(
        "--proxy-port", type=int, default=None,
        help="Proxy port override (default from lsf.LAB_PROXY_PORT)",
    )
    p.add_argument(
        "--clear",
        action="store_true",
        help="Write no-proxy policy variant (non-HOL lab types)",
    )
    p.add_argument(
        "--console-host",
        default="root@console.site-a.vcf.lab",
        help="SSH target for the root-owned policies.json/environment writes",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-h", "--help", action="store_true", default=argparse.SUPPRESS)

    try:
        args = p.parse_args()
    except SystemExit:
        sys.exit(1)

    zoom_val = args.zoom
    if zoom_val > 5.0:  # e.g. 80 passed instead of 0.8
        zoom_val = zoom_val / 100.0

    lsf.init(router=False)
    # Minimal shim: forwards the attributes consumed by apply_firefox_lmchol_tuning
    # and _build_firefox_no_proxy so the canonical lsf values are always used.
    class _Shim:
        mc = args.mc_base
        LAB_PROXY_IP = lsf.LAB_PROXY_IP
        LAB_PROXY_PORT = lsf.LAB_PROXY_PORT
        LAB_NO_PROXY_PARTS = lsf.LAB_NO_PROXY_PARTS
        write_output = staticmethod(print)
        get_password = staticmethod(lsf.get_password)
        set_console_firefox_policies = staticmethod(lsf.set_console_firefox_policies)
        set_console_crashreporter_env = staticmethod(lsf.set_console_crashreporter_env)

    success = apply_firefox_lmchol_tuning(
        _Shim(),
        dry_run=args.dry_run,
        clear=args.clear,
        proxy_host=args.proxy_host,
        proxy_port=args.proxy_port,
        console_host=args.console_host,
        zoom_level=zoom_val,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

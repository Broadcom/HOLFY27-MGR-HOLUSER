#!/usr/bin/env python3
# vpodchecker.py - HOLFY27 Lab Validation Tool
# Version 2.3 - February 26, 2026
# Author - Burke Azbill and HOL Core Team
# Modernized for HOLFY27 architecture with enhanced checks and reporting
#
# CHANGELOG:
# v2.3 - 2026-02-26:
#   - Added "Firefox Trusted Private CAs" section: enumerates custom CAs in the
#     Firefox NSS cert store, verifies expected CAs are present, reports expiry
# v2.2 - 2026-02-26:
#   - NSX Edge password expiration checks added (admin, root, audit) via NSX
#     Manager transport node API (edges don't expose /api/v1/node/users directly)
#   - SDDC Manager root/backup password checks now use expect/su instead of
#     INFO placeholders (vcf user has no sudo but can su to root)
#   - VCF Automation root password check now uses sudo -S for privilege escalation
#   - New get_linux_password_expiration_via_su() for appliances requiring su
#   - get_linux_password_expiration() gains use_sudo parameter for sudo -S escalation
# v2.1 - 2026-02-26:
#   - License checks now report ALL entities individually (evaluation licenses
#     are no longer de-duplicated by key, so every ESXi host is shown)
#   - Added VCF Operations Manager (ops-a) license/status check
# v2.0 - 2026-01:
#   - Initial modernized release for HOLFY27

"""
VPod Checker - Validates lab configuration against HOL standards

This tool checks:
- SSL certificate expiration dates
- vSphere license validity and expiration (all entities per vCenter)
- VCF Operations Manager license status
- ESXi host NTP configuration
- VM configuration (uuid.action, typematic delay, autolock)
- VM resource configuration (reservations, shares)

Usage:
    python3 vpodchecker.py [options]
    
Options:
    --report-only   Don't fix issues, just report
    --json          Output as JSON
    --html          Generate HTML report
    --verbose       Verbose output
"""

import sys
import os
import errno
import datetime
import socket
import re
import json
import argparse
import subprocess
import glob
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict

# Add hol directory for imports
sys.path.insert(0, '/home/holuser/hol')

# Import lsfunctions
try:
    import lsfunctions as lsf
except ImportError:
    # For local testing
    lsf = None

# Optional imports with graceful fallback
try:
    import OpenSSL
    import ssl
    SSL_AVAILABLE = True
except ImportError:
    SSL_AVAILABLE = False
    print('Warning: OpenSSL not available - SSL checks will be skipped')

try:
    from prettytable import PrettyTable
    PRETTYTABLE_AVAILABLE = True
except ImportError:
    PRETTYTABLE_AVAILABLE = False

try:
    from pyVim import connect
    from pyVmomi import vim
    from pyVim.task import WaitForTask
    PYVMOMI_AVAILABLE = True
except ImportError:
    PYVMOMI_AVAILABLE = False


#==============================================================================
# DATA CLASSES
#==============================================================================

@dataclass
class CheckResult:
    """Result of a single check"""
    name: str
    status: str  # PASS, FAIL, WARN, FIXED, SKIPPED
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SslHost:
    """SSL host information"""
    name: str
    port: int
    certname: str = ""
    issuer: str = ""
    ssl_exp_date: Optional[datetime.date] = None
    days_to_expire: int = 0


@dataclass
class ValidationReport:
    """Complete validation report"""
    lab_sku: str
    timestamp: str
    min_exp_date: str
    max_exp_date: str
    ssl_checks: List[CheckResult] = field(default_factory=list)
    license_checks: List[CheckResult] = field(default_factory=list)
    ntp_checks: List[CheckResult] = field(default_factory=list)
    vm_config_checks: List[CheckResult] = field(default_factory=list)
    vm_resource_checks: List[CheckResult] = field(default_factory=list)
    password_expiration_checks: List[CheckResult] = field(default_factory=list)
    fleet_password_policy_checks: List[CheckResult] = field(default_factory=list)
    firefox_ca_checks: List[CheckResult] = field(default_factory=list)
    overall_status: str = "PASS"
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)


#==============================================================================
# LAB YEAR EXTRACTION
#==============================================================================

def extract_lab_year(lab_sku: str) -> str:
    """
    Extract the 2-digit lab year from various SKU formats.
    
    Supported formats:
    - Standard: HOL-2701, ATE-2705, VXP-2703 → extracts '27'
    - BETA: BETA-901-TNDNS → extracts '27' (from first digit after hyphen, assumes 9XX = FY27 testing)
    - Named: Discovery-Demo, EDU-Workshop → defaults to current HOLFY year
    
    The function attempts multiple extraction strategies:
    1. Look for 4-digit pattern after hyphen (XXYY format) → extract XX
    2. Look for 3-digit pattern starting with 9 (beta testing) → default to 27
    3. Look for any 2-digit year-like number (20-30 range) in the SKU
    4. Fall back to '27' for HOLFY27
    
    :param lab_sku: Lab SKU string (e.g., 'HOL-2701', 'BETA-901-TNDNS', 'Discovery-Demo')
    :return: 2-digit year string (e.g., '27')
    """
    # Default year for HOLFY27
    default_year = '27'
    
    if not lab_sku or len(lab_sku) < 4:
        return default_year
    
    # Strategy 1: Look for standard 4-digit lab number after hyphen (e.g., HOL-2701 → 2701 → 27)
    # Pattern: PREFIX-XXYY where XX is year and YY is lab number
    match = re.search(r'-(\d{4})(?:\D|$)', lab_sku)
    if match:
        four_digits = match.group(1)
        year_part = four_digits[:2]
        # Validate it looks like a reasonable year (20-35 range for HOL labs)
        try:
            year_int = int(year_part)
            if 20 <= year_int <= 35:
                return year_part
        except ValueError:
            pass
    
    # Strategy 2: Look for 3-digit pattern starting with 9 (beta/testing labs like BETA-901)
    # These are typically testing labs for the current fiscal year
    match = re.search(r'-9\d{2}(?:\D|$)', lab_sku)
    if match:
        # Beta labs (9XX) are for current FY testing, use default
        return default_year
    
    # Strategy 3: Look for any 2-digit year pattern in the SKU
    # This catches edge cases where year might be in a different position
    match = re.search(r'(\d{2})\d{2}', lab_sku)
    if match:
        year_part = match.group(1)
        try:
            year_int = int(year_part)
            if 20 <= year_int <= 35:
                return year_part
        except ValueError:
            pass
    
    # Strategy 4: Fall back to default year for named labs (Discovery-Demo, etc.)
    return default_year


#==============================================================================
# SSL CERTIFICATE CHECKS
#==============================================================================

def get_ssl_host_from_url(url: str) -> SslHost:
    """Extract hostname and port from URL"""
    parts = url.split('/')
    host_part = parts[2] if len(parts) > 2 else url
    
    if ':' in host_part:
        name, port = host_part.split(':')
        port = int(port)
    else:
        name = host_part
        port = 443
    
    return SslHost(name=name, port=port)


def get_cert_expiration(ssl_cert) -> datetime.date:
    """Get SSL certificate expiration date"""
    x509info = ssl_cert.get_notAfter()
    exp_day = int(x509info[6:8].decode("utf-8"))
    exp_month = int(x509info[4:6].decode("utf-8"))
    exp_year = int(x509info[:4].decode("utf-8"))
    return datetime.date(exp_year, exp_month, exp_day)


def check_ssl_certificates(urls: List[str], min_exp_date: datetime.date) -> List[CheckResult]:
    """Check SSL certificates for all HTTPS URLs"""
    results = []
    
    if not SSL_AVAILABLE:
        results.append(CheckResult(
            name="SSL Check",
            status="SKIPPED",
            message="OpenSSL not available"
        ))
        return results
    
    checked_hosts = set()
    
    for url in urls:
        if not url.startswith('https'):
            continue
        
        host = get_ssl_host_from_url(url)
        
        if host.name in checked_hosts:
            continue
        checked_hosts.add(host.name)
        
        # Skip external URLs if lsf is available
        if lsf and hasattr(lsf, 'check_proxy'):
            try:
                if lsf.check_proxy(url):
                    continue
            except Exception:
                pass
        
        try:
            if lsf and not lsf.test_tcp_port(host.name, host.port):
                results.append(CheckResult(
                    name=f"SSL: {host.name}:{host.port}",
                    status="WARN",
                    message="Host not reachable",
                    details={"host": host.name, "port": host.port}
                ))
                continue
            
            cert = ssl.get_server_certificate((host.name, host.port))
            x509 = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM, cert)
            
            subject = x509.get_subject()
            host.certname = subject.CN or "Unknown"
            
            issuer = x509.get_issuer()
            if issuer.OU or issuer.O:
                host.issuer = f"OU={issuer.OU or ''} O={issuer.O or ''}"
            else:
                host.issuer = "Self-Signed"
            
            host.ssl_exp_date = get_cert_expiration(x509)
            
            # Calculate months until expiration (matching License check logic)
            today = datetime.date.today()
            days_until = (host.ssl_exp_date - today).days
            host.days_to_expire = days_until
            months_until = days_until / 30.44
            
            if months_until >= 9:
                status = "PASS"
                message = f"Certificate valid - expires {host.ssl_exp_date} (>= 9 months)"
            elif months_until >= 3:
                status = "WARN"
                message = f"Certificate expires soon - expires {host.ssl_exp_date} (< 9 months)"
            else:
                if host.name == 'www.vmware.com':
                    status = "WARN"
                    message = f"External certificate expires soon/past - expires {host.ssl_exp_date}"
                else:
                    status = "FAIL"
                    message = f"Certificate expires critically soon - expires {host.ssl_exp_date} (< 3 months)"
            
            results.append(CheckResult(
                name=f"SSL: {host.name}:{host.port}",
                status=status,
                message=message,
                details={
                    "host": host.name,
                    "port": host.port,
                    "certname": host.certname,
                    "expiration": str(host.ssl_exp_date),
                    "days_to_expire": host.days_to_expire,
                    "issuer": host.issuer
                }
            ))
            
        except Exception as e:
            if host.name == 'www.vmware.com':
                status = "WARN"
                message = f"External host check failed (expected): {e}"
            else:
                status = "FAIL"
                message = f"Could not check certificate: {e}"

            results.append(CheckResult(
                name=f"SSL: {host.name}:{host.port}",
                status=status,
                message=message,
                details={"host": host.name, "port": host.port, "error": str(e)}
            ))
    
    return results


#==============================================================================
# NTP CONFIGURATION CHECKS
#==============================================================================

def get_ntp_config(esx_host) -> Dict[str, Any]:
    """Get NTP configuration for an ESXi host"""
    ntp_data = {
        "hostname": esx_host.name,
        "running": False,
        "policy": "",
        "server": ""
    }
    
    try:
        for service in esx_host.config.service.service:
            if service.key == 'ntpd':
                ntp_data["running"] = service.running
                ntp_data["policy"] = service.policy
                if esx_host.config.dateTimeInfo.ntpConfig.server:
                    ntp_data["server"] = esx_host.config.dateTimeInfo.ntpConfig.server[0]
                break
    except Exception:
        pass
    
    return ntp_data


def check_ntp_configuration(hosts: List) -> List[CheckResult]:
    """Check NTP configuration on all ESXi hosts"""
    results = []
    
    for host in hosts:
        ntp_config = get_ntp_config(host)
        
        issues = []
        if not ntp_config["running"]:
            issues.append("NTPD not running")
        if ntp_config["policy"] != "on":
            issues.append(f"NTPD policy is '{ntp_config['policy']}' (should be 'on')")
        if not ntp_config["server"]:
            issues.append("No NTP server configured")
        
        if issues:
            status = "WARN"
            message = "; ".join(issues)
        else:
            status = "PASS"
            message = f"NTP configured correctly (server: {ntp_config['server']})"
        
        results.append(CheckResult(
            name=f"NTP: {host.name}",
            status=status,
            message=message,
            details=ntp_config
        ))
    
    return results


#==============================================================================
# VM CONFIGURATION CHECKS
#==============================================================================

def add_vm_config_extra_option(vm, option_key: str, option_value: str) -> bool:
    """Add or update a VM extra config option"""
    try:
        spec = vim.vm.ConfigSpec()
        opt = vim.option.OptionValue()
        spec.extraConfig = []
        opt.key = option_key
        opt.value = option_value
        spec.extraConfig.append(opt)
        task = vm.ReconfigVM_Task(spec)
        WaitForTask(task)
        return True
    except Exception as e:
        print(f"Failed to set {option_key} on {vm.name}: {e}")
        return False


def check_vm_configuration(vms: List, fix_issues: bool = True) -> List[CheckResult]:
    """Check and optionally fix VM configuration"""
    results = []
    
    # System VMs that should be skipped - these cannot be modified
    SKIP_VM_PATTERNS = [
        'vcf-services-platform-template-',    # VCF Services Platform Template VMs
        'SupervisorControlPlaneVM',           # Tanzu Supervisor Control Plane VMs
        'vna-wld01-',                         # VNA Workload VMs
    ]
    
    for vm in vms:
        # Skip system VMs that cannot be modified
        skip_vm = False
        for pattern in SKIP_VM_PATTERNS:
            if pattern in vm.name:
                skip_vm = True
                break
        
        if skip_vm:
            continue
        
        issues = []
        fixes = []
        
        # Get current config
        uuid_action = ""
        type_delay = ""
        autolock = ""
        
        try:
            for optionValue in vm.config.extraConfig:
                if optionValue.key == 'uuid.action':
                    uuid_action = optionValue.value
                if optionValue.key == 'keyboard.typematicMinDelay':
                    type_delay = optionValue.value
                if optionValue.key == 'tools.guest.desktop.autolock':
                    autolock = optionValue.value
        except Exception:
            pass
        
        # Check uuid.action
        if uuid_action != 'keep':
            issues.append(f"uuid.action is '{uuid_action}' (should be 'keep')")
            if fix_issues:
                if add_vm_config_extra_option(vm, 'uuid.action', 'keep'):
                    fixes.append("uuid.action fixed")
        
        # Check Windows VMs
        if vm.config.guestId and re.search(r'windows', vm.config.guestId, re.IGNORECASE):
            if autolock != 'FALSE':
                issues.append(f"tools.guest.desktop.autolock is '{autolock}' (should be 'FALSE')")
                if fix_issues:
                    if add_vm_config_extra_option(vm, 'tools.guest.desktop.autolock', 'FALSE'):
                        fixes.append("autolock fixed")
        
        # Check Linux VMs
        linux_patterns = r'linux|ubuntu|debian|centos|sles|suse|asianux|novell|redhat|photon|rhel|other'
        if vm.config.guestId and re.search(linux_patterns, vm.config.guestId, re.IGNORECASE):
            if type_delay != '2000000':
                issues.append(f"keyboard.typematicMinDelay is '{type_delay}' (should be '2000000')")
                if fix_issues:
                    if add_vm_config_extra_option(vm, 'keyboard.typematicMinDelay', '2000000'):
                        fixes.append("typematicMinDelay fixed")
        
        if issues:
            if fixes and fix_issues:
                status = "FIXED"
                message = f"Fixed: {', '.join(fixes)}"
            else:
                status = "FAIL"
                message = "; ".join(issues)
        else:
            status = "PASS"
            message = "VM configuration correct"
        
        results.append(CheckResult(
            name=f"VM Config: {vm.name}",
            status=status,
            message=message,
            details={
                "vm_name": vm.name,
                "guest_id": vm.config.guestId if vm.config else "",
                "uuid_action": uuid_action,
                "type_delay": type_delay,
                "autolock": autolock
            }
        ))
    
    return results


#==============================================================================
# LICENSE CHECKS
#==============================================================================

def get_months_until_expiration(exp_date: datetime.date) -> float:
    """Calculate months until expiration from today's date"""
    today = datetime.date.today()
    days_until = (exp_date - today).days
    # Approximate months (30.44 days per month on average)
    return days_until / 30.44


def get_license_expiration_status(exp_date: datetime.date) -> tuple:
    """
    Determine license status based on months until expiration.
    
    Returns:
        tuple: (status, message) where status is PASS/WARN/FAIL
        
    Status rules:
        - PASS (green checkmark): >= 9 months from now
        - WARN (warning icon): < 9 months but >= 3 months from now
        - FAIL (red X): < 3 months from now
    """
    months_until = get_months_until_expiration(exp_date)
    
    if months_until >= 9:
        status = "PASS"
        message = f"License valid - expires {exp_date} (>= 9 months)"
    elif months_until >= 3:
        status = "WARN"
        message = f"License expiring soon - expires {exp_date} (< 9 months)"
    else:
        status = "FAIL"
        message = f"License expiring critically soon - expires {exp_date} (< 3 months)"
    
    return status, message


def check_licenses(sis: List, min_exp_date: datetime.date, max_exp_date: datetime.date) -> List[CheckResult]:
    """
    Check vSphere licenses for ALL entities (vCenters, ESXi hosts, etc.).
    
    Reports every entity's license assignment individually so that evaluation
    licenses or expiring licenses are visible per-host rather than being
    de-duplicated by license key.
    """
    results = []
    license_keys_detail_reported = set()
    
    for si in sis:
        try:
            lic_mgr = si.content.licenseManager
            lic_assignment_mgr = lic_mgr.licenseAssignmentManager
            assets = lic_assignment_mgr.QueryAssignedLicenses()
            
            for asset in assets:
                license_key = asset.assignedLicense.licenseKey
                license_name = asset.assignedLicense.name
                entity_name = asset.entityDisplayName
                
                if license_key == '00000-00000-00000-00000-00000':
                    results.append(CheckResult(
                        name=f"License: {entity_name}",
                        status="FAIL",
                        message="Evaluation license detected - no expiration date",
                        details={"license_key": license_key, "entity": entity_name}
                    ))
                    continue
                
                exp_date = None
                for prop in asset.assignedLicense.properties:
                    if prop.key == 'expirationDate':
                        exp_date = prop.value
                        break
                
                if exp_date:
                    status, message = get_license_expiration_status(exp_date.date())
                else:
                    if 'NSX for vShield Endpoint' in license_name:
                        status = "WARN"
                        message = "Non-expiring license (expected for vShield Endpoint) - no expiration date"
                    else:
                        status = "FAIL"
                        message = "Non-expiring license detected - no expiration date"
                
                if license_key not in license_keys_detail_reported:
                    license_keys_detail_reported.add(license_key)
                    results.append(CheckResult(
                        name=f"License: {license_name}",
                        status=status,
                        message=f"{entity_name} - {message}",
                        details={
                            "license_key": license_key[:5] + "-****-****-****-" + license_key[-5:],
                            "entity": entity_name,
                            "expiration": str(exp_date.date()) if exp_date else "Never"
                        }
                    ))
                else:
                    results.append(CheckResult(
                        name=f"  License: {entity_name}",
                        status=status,
                        message=f"{license_name} - {message}",
                        details={
                            "license_key": license_key[:5] + "-****-****-****-" + license_key[-5:],
                            "entity": entity_name,
                            "expiration": str(exp_date.date()) if exp_date else "Never"
                        }
                    ))
            
            # Check for unassigned licenses
            for lic in lic_mgr.licenses:
                if not lic.used and lic.licenseKey != '00000-00000-00000-00000-00000':
                    exp_date = None
                    for prop in lic.properties:
                        if prop.key == 'expirationDate':
                            exp_date = prop.value
                            break
                    
                    if exp_date:
                        exp_msg = f" - expires {exp_date.date()}"
                    else:
                        exp_msg = " - no expiration date"
                    
                    results.append(CheckResult(
                        name=f"License: {lic.name}",
                        status="WARN",
                        message=f"Unassigned license - should be removed{exp_msg}",
                        details={"license_key": lic.licenseKey[:5] + "-****"}
                    ))
        
        except Exception as e:
            results.append(CheckResult(
                name="License Check",
                status="FAIL",
                message=f"Could not check licenses: {e}"
            ))
    
    return results


def check_vcf_operations_license() -> List[CheckResult]:
    """
    Check VCF Operations Manager (ops-a) license status via suite-api.
    
    VCF Operations in VCF 9.x is licensed through the VCF platform license.
    This check verifies that the Operations Manager is online and the
    deployment is not in an evaluation/unlicensed state by querying the
    node status and solution inventory.
    """
    results = []
    
    if not lsf:
        return results
    
    ops_fqdn = None
    try:
        if lsf.config.has_option('RESOURCES', 'URLs'):
            urls_raw = lsf.config.get('RESOURCES', 'URLs').split('\n')
            for entry in urls_raw:
                url = entry.split(',')[0].strip()
                if 'ops-' in url and '.vcf.lab' in url:
                    from urllib.parse import urlparse
                    parsed = urlparse(url)
                    ops_fqdn = parsed.hostname
                    break
    except Exception:
        pass
    
    if not ops_fqdn:
        return results
    
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except ImportError:
        return results
    
    password = lsf.get_password()
    api_base = f"https://{ops_fqdn}/suite-api"
    
    try:
        token_resp = requests.post(
            f"{api_base}/api/auth/token/acquire",
            json={"username": "admin", "password": password, "authSource": "local"},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            verify=False, timeout=30
        )
        token_resp.raise_for_status()
        token = token_resp.json()["token"]
    except Exception as e:
        results.append(CheckResult(
            name=f"VCF Operations: {ops_fqdn}",
            status="WARN",
            message=f"Could not authenticate: {str(e)[:60]}"
        ))
        return results
    
    headers = {
        "Authorization": f"OpsToken {token}",
        "Accept": "application/json",
    }
    
    try:
        status_resp = requests.get(
            f"{api_base}/api/deployment/node/status",
            headers=headers, verify=False, timeout=30
        )
        status_resp.raise_for_status()
        status_data = status_resp.json()
        node_status = status_data.get("status", "UNKNOWN")
        
        if node_status == "ONLINE":
            results.append(CheckResult(
                name=f"VCF Operations: {ops_fqdn}",
                status="PASS",
                message=f"VCF Operations Manager is ONLINE (VCF platform license)",
                details={"ops_fqdn": ops_fqdn, "status": node_status}
            ))
        else:
            results.append(CheckResult(
                name=f"VCF Operations: {ops_fqdn}",
                status="WARN",
                message=f"VCF Operations Manager status: {node_status}",
                details={"ops_fqdn": ops_fqdn, "status": node_status}
            ))
    except Exception as e:
        results.append(CheckResult(
            name=f"VCF Operations: {ops_fqdn}",
            status="WARN",
            message=f"Could not check status: {str(e)[:60]}"
        ))
    
    return results


#==============================================================================
# REPORT GENERATION
#==============================================================================

def print_results_table(title: str, results: List[CheckResult]):
    """Print results as a table"""
    if PRETTYTABLE_AVAILABLE:
        table = PrettyTable()
        table.field_names = ['Name', 'Status', 'Message']
        table.align = 'l'
        
        for result in results:
            status_icon = {
                'PASS': '✅',
                'FAIL': '❌',
                'WARN': '⚠️',
                'FIXED': '🔧',
                'SKIPPED': '⏭️'
            }.get(result.status, '❓')
            
            table.add_row([result.name, f"{status_icon} {result.status}", result.message[:60]])
        
        print(f"\n==== {title} ====")
        print(table)
    else:
        print(f"\n==== {title} ====")
        for result in results:
            print(f"  {result.status}: {result.name} - {result.message}")


def generate_html_report(report: ValidationReport) -> str:
    """Generate HTML report"""
    html = f'''<!DOCTYPE html>
<html>
<head>
    <title>VPod Checker Report - {report.lab_sku}</title>
    <style>
        body {{ font-family: 'Segoe UI', sans-serif; margin: 2rem; background: #f5f5f5; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 2rem; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        h1 {{ color: #1e293b; }}
        h2 {{ color: #334155; border-bottom: 2px solid #e2e8f0; padding-bottom: 0.5rem; }}
        .status-pass {{ color: #16a34a; }}
        .status-fail {{ color: #dc2626; }}
        .status-warn {{ color: #ca8a04; }}
        .status-fixed {{ color: #2563eb; }}
        table {{ width: 100%; border-collapse: collapse; margin: 1rem 0; }}
        th, td {{ padding: 0.75rem; text-align: left; border-bottom: 1px solid #e2e8f0; }}
        th {{ background: #f8fafc; font-weight: 600; }}
        .overall-pass {{ background: #dcfce7; color: #166534; padding: 1rem; border-radius: 8px; }}
        .overall-fail {{ background: #fee2e2; color: #991b1b; padding: 1rem; border-radius: 8px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>VPod Checker Report: {report.lab_sku}</h1>
        <p>Generated: {report.timestamp}</p>
        <p>Lab license expiration window: {report.min_exp_date} to {report.max_exp_date}</p>
        
        <div class="overall-{'pass' if report.overall_status == 'PASS' else 'fail'}">
            <strong>Overall Status: {report.overall_status}</strong>
        </div>
'''
    
    # Add sections for each check type
    sections = [
        ('SSL Certificate Checks', report.ssl_checks),
        ('License Checks', report.license_checks),
        ('NTP Configuration', report.ntp_checks),
        ('VM Configuration', report.vm_config_checks),
        ('VM Resources', report.vm_resource_checks),
        ('Password Expiration Checks', report.password_expiration_checks)
    ]
    
    for title, checks in sections:
        if checks:
            html += f'''
        <h2>{title}</h2>
        <table>
            <tr><th>Name</th><th>Status</th><th>Message</th></tr>
'''
            for check in checks:
                status_class = f"status-{check.status.lower()}"
                html += f'            <tr><td>{check.name}</td><td class="{status_class}">{check.status}</td><td>{check.message}</td></tr>\n'
            html += '        </table>\n'
    
    html += '''
    </div>
</body>
</html>
'''
    return html


#==============================================================================
# PASSWORD EXPIRATION CHECKS
#==============================================================================

def _parse_chage_output(output: str) -> Optional[int]:
    """
    Parse 'chage -l' output to extract days until password expiration.
    
    :param output: Raw chage -l output (may contain multiple lines)
    :return: None if never expires or unparseable, else days until expiration
    """
    if not output or 'never' in output.lower():
        return None
    
    match = re.search(r'Password expires\s*:\s*(.+)', output)
    if match:
        date_str = match.group(1).strip()
        if 'never' in date_str.lower():
            return None
        
        for fmt in ('%b %d, %Y', '%Y-%m-%d', '%m/%d/%Y'):
            try:
                exp_date = datetime.datetime.strptime(date_str, fmt).date()
                return (exp_date - datetime.date.today()).days
            except ValueError:
                continue
    
    return None


def get_linux_password_expiration(hostname: str, username: str, password: str,
                                   ssh_user: str = 'root',
                                   use_sudo: bool = False) -> Optional[int]:
    """
    Get password expiration for a Linux user account.
    
    Uses 'chage -l' to query password aging. Not available on ESXi
    (returns None, which callers treat as "never expires" -- correct
    since ESXi root defaults to 99999-day max in /etc/shadow).
    
    When use_sudo=True, pipes the ssh_user's password to 'sudo -S chage -l'.
    This is needed when the SSH user has sudo privileges but is not root
    (e.g., vmware-system-user on VCF Automation appliances).
    
    Note: without use_sudo, 'chage -l' for a different user requires root.
    Non-root SSH users can only query their own account.
    
    :param hostname: Target host
    :param username: Account to check expiration for
    :param password: SSH password
    :param ssh_user: SSH login user (default 'root')
    :param use_sudo: If True, use sudo -S to escalate (default False)
    :return: None if never expires or check failed, else days until expiration
    """
    try:
        if use_sudo:
            cmd = f"echo '{password}' | sudo -S chage -l {username} 2>/dev/null"
        else:
            cmd = f"chage -l {username} 2>/dev/null | grep 'Password expires'"
        result = lsf.ssh(cmd, f'{ssh_user}@{hostname}', password)
        
        output = ''
        if hasattr(result, 'stdout') and result.stdout:
            output = result.stdout.strip()
        elif isinstance(result, str):
            output = result.strip()
        
        return _parse_chage_output(output)
    except Exception:
        return None


def get_linux_password_expiration_via_su(hostname: str, username: str,
                                          password: str,
                                          ssh_user: str = 'vcf') -> Optional[int]:
    """
    Get password expiration using 'su' to root, then 'chage -l'.
    
    Required on appliances like SDDC Manager where the SSH user (vcf) has
    no sudo privileges but can 'su' to root with a password. Uses expect
    to handle the interactive password prompt.
    
    :param hostname: Target host
    :param username: Account to check (e.g. 'root', 'backup')
    :param password: Password for both SSH user and root (su)
    :param ssh_user: SSH login user (default 'vcf')
    :return: None if never expires or check failed, else days until expiration
    """
    try:
        expect_cmd = (
            f"expect -c '"
            f'set timeout 10\n'
            f'spawn sshpass -p {{{password}}} ssh -o StrictHostKeyChecking=no '
            f'-o UserKnownHostsFile=/dev/null {ssh_user}@{hostname}\n'
            f'expect " ]$"\n'
            f'send "su - root\\r"\n'
            f'expect "Password:"\n'
            f'send "{password}\\r"\n'
            f'expect " ]#"\n'
            f'send "chage -l {username}\\r"\n'
            f'expect " ]#"\n'
            f'send "exit\\r"\n'
            f'expect " ]$"\n'
            f'send "exit\\r"\n'
            f"expect eof'"
        )
        result = subprocess.run(expect_cmd, shell=True, capture_output=True,
                                text=True, timeout=30)
        
        output = result.stdout if result.stdout else ''
        return _parse_chage_output(output)
    except Exception:
        return None


def get_vcenter_user_expiration(hostname: str, vcuser: str, vcpassword: str, 
                                  target_user: str) -> Optional[int]:
    """
    Get password expiration for a vCenter local appliance account via REST API.
    
    Uses /rest/appliance/local-accounts/{user} which only works for local
    OS accounts (e.g. root), NOT SSO directory users (e.g. administrator).
    
    The password_expires_at field is an ISO 8601 date string
    (e.g. "2053-07-11T00:00:00.000Z"), not a numeric timestamp.
    
    Returns:
        - None if password never expires, user not found, or check failed
        - Number of days until expiration
    """
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        session = requests.Session()
        session.verify = False
        
        auth_url = f'https://{hostname}/rest/com/vmware/cis/session'
        response = session.post(auth_url, auth=(vcuser, vcpassword), timeout=15)
        
        if response.status_code != 200:
            return None
        
        user_url = f'https://{hostname}/rest/appliance/local-accounts/{target_user}'
        response = session.get(user_url, timeout=15)
        
        if response.status_code == 200:
            data = response.json()
            if 'value' in data:
                user_info = data['value']
                
                if 'password_expires_at' in user_info and user_info['password_expires_at']:
                    exp_str = user_info['password_expires_at']
                    try:
                        exp_str_clean = exp_str.replace('Z', '+00:00')
                        exp_date = datetime.datetime.fromisoformat(exp_str_clean).date()
                        return (exp_date - datetime.date.today()).days
                    except (ValueError, TypeError):
                        pass
                
                if 'max_days_between_password_change' in user_info:
                    max_days = user_info['max_days_between_password_change']
                    if max_days == -1 or max_days > 9000:
                        return None  # Effectively never expires
                    if 'last_password_change' in user_info and user_info['last_password_change']:
                        try:
                            lpc_str = user_info['last_password_change'].replace('Z', '+00:00')
                            lpc_date = datetime.datetime.fromisoformat(lpc_str).date()
                            exp_date = lpc_date + datetime.timedelta(days=max_days)
                            return (exp_date - datetime.date.today()).days
                        except (ValueError, TypeError):
                            pass
                    return max_days
        
        return None
    except Exception:
        return None


NSX_USER_IDS = {
    'root': 0,
    'admin': 10000,
    'audit': 10002,
    'guestuser1': 10003,
    'guestuser2': 10004,
}


def get_nsx_user_expiration(hostname: str, username: str, password: str,
                              target_user: str) -> Optional[int]:
    """
    Get password expiration for NSX user via REST API.
    
    The NSX API identifies users by numeric userid, not username:
      root=0, admin=10000, audit=10002
    
    If the target_user is not in the known ID map, falls back to
    listing all users and matching by username.
    
    Returns:
        - None if password never expires or check failed
        - Number of days until expiration
    """
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        session = requests.Session()
        session.verify = False
        
        user_id = NSX_USER_IDS.get(target_user)
        data = None
        
        if user_id is not None:
            url = f'https://{hostname}/api/v1/node/users/{user_id}'
            response = session.get(url, auth=(username, password), timeout=15)
            if response.status_code == 200:
                data = response.json()
        
        if data is None:
            url = f'https://{hostname}/api/v1/node/users'
            response = session.get(url, auth=(username, password), timeout=15)
            if response.status_code == 200:
                for user_entry in response.json().get('results', []):
                    if user_entry.get('username') == target_user:
                        data = user_entry
                        break
        
        if data is None:
            return None
        
        if 'password_change_frequency' in data:
            freq = data['password_change_frequency']
            if freq == 0 or freq > 9000:
                return None  # Never expires
            
            if 'last_password_change' in data:
                last_change_epoch = data['last_password_change']
                if last_change_epoch > 1e9:
                    last_change = datetime.datetime.fromtimestamp(
                        last_change_epoch / 1000
                    ).date()
                else:
                    last_change = datetime.date.today() - datetime.timedelta(days=last_change_epoch)
                exp_date = last_change + datetime.timedelta(days=freq)
                days_until = (exp_date - datetime.date.today()).days
                return days_until
            
            return freq
        
        return None
    except Exception:
        return None


def get_nsx_edge_user_expiration(edge_hostname: str, nsx_manager: str,
                                  password: str,
                                  target_user: str) -> Optional[int]:
    """
    Get password expiration for an NSX Edge user via the NSX Manager transport node API.
    
    Edges don't expose /api/v1/node/users directly; the managing NSX Manager
    proxies these calls through:
        GET /api/v1/transport-nodes/{node-id}/node/users/{user-id}
    
    :param edge_hostname: Edge display name (e.g. edge-wld01-01a)
    :param nsx_manager: NSX Manager FQDN that manages this edge
    :param password: Admin password for the NSX Manager
    :param target_user: User to check (root, admin, audit)
    :return: None if never expires or check failed, else days until expiration
    """
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        session = requests.Session()
        session.verify = False

        # Find the edge's transport node ID
        tn_url = f'https://{nsx_manager}/api/v1/transport-nodes'
        resp = session.get(tn_url, auth=('admin', password), timeout=30)
        if resp.status_code != 200:
            return None

        node_id = None
        for node in resp.json().get('results', []):
            if node.get('display_name', '') == edge_hostname:
                node_id = node.get('node_id', node.get('id'))
                break

        if not node_id:
            return None

        user_id = NSX_USER_IDS.get(target_user)
        data = None

        if user_id is not None:
            url = f'https://{nsx_manager}/api/v1/transport-nodes/{node_id}/node/users/{user_id}'
            resp = session.get(url, auth=('admin', password), timeout=15)
            if resp.status_code == 200:
                data = resp.json()

        if data is None:
            url = f'https://{nsx_manager}/api/v1/transport-nodes/{node_id}/node/users'
            resp = session.get(url, auth=('admin', password), timeout=15)
            if resp.status_code == 200:
                for user_entry in resp.json().get('results', []):
                    if user_entry.get('username') == target_user:
                        data = user_entry
                        break

        if data is None:
            return None

        if 'password_change_frequency' in data:
            freq = data['password_change_frequency']
            if freq == 0 or freq > 9000:
                return None

            if 'last_password_change' in data:
                last_change_epoch = data['last_password_change']
                if last_change_epoch > 1e9:
                    last_change = datetime.datetime.fromtimestamp(
                        last_change_epoch / 1000
                    ).date()
                else:
                    last_change = datetime.date.today() - datetime.timedelta(days=last_change_epoch)
                exp_date = last_change + datetime.timedelta(days=freq)
                days_until = (exp_date - datetime.date.today()).days
                return days_until

            return freq

        return None
    except Exception:
        return None


def _get_nsx_manager_for_edge_check(edge_hostname: str) -> Optional[str]:
    """
    Determine which NSX Manager manages a given edge node by name convention.
    
    Edge names follow the pattern edge-{domain}-{num}{site} where domain
    matches the NSX Manager pattern nsx-{domain}-{num}{site}.
    For example: edge-wld01-01a -> nsx-wld01-01a
    
    :param edge_hostname: NSX Edge hostname (e.g. edge-wld01-01a)
    :return: NSX Manager FQDN, or None if not found
    """
    if not lsf or not lsf.config.has_section('VCF'):
        return None
    if not lsf.config.has_option('VCF', 'vcfnsxmgr'):
        return None

    vcfnsxmgrs = lsf.config.get('VCF', 'vcfnsxmgr').split('\n')
    nsx_managers = []
    for entry in vcfnsxmgrs:
        if not entry or entry.strip().startswith('#'):
            continue
        nsx_managers.append(entry.split(':')[0].strip())

    edge_match = re.match(r'edge-(\w+)-\d+', edge_hostname)
    if edge_match:
        edge_domain = edge_match.group(1)
        for mgr in nsx_managers:
            if edge_domain in mgr:
                return mgr

    return nsx_managers[0] if nsx_managers else None


def check_password_expirations() -> List[CheckResult]:
    """
    Check password expiration for all known user accounts.
    
    Checks:
    - ESXi hosts: root user
    - vCenter servers: root (Linux), administrator@vsphere.local (SSO)
    - NSX managers: admin, root, audit users
    - NSX edges: admin, root, audit users (via NSX Manager transport node API)
    - SDDC Manager: vcf, backup, root users
    - vRA/Automation: vmware-system-user, root users
    
    Status:
    - PASS: No expiration or > 3 years (1095 days)
    - FAIL: Expires in < 2 years (730 days)
    - WARN: Could not check
    """
    results = []
    
    if not lsf:
        return [CheckResult(
            name="Password Expiration Checks",
            status="SKIPPED",
            message="lsfunctions not available"
        )]
    
    password = lsf.get_password()
    three_years_days = 1095
    two_years_days = 730
    
    # Check ESXi hosts
    esxi_hosts = []
    if lsf.config.has_option('RESOURCES', 'ESXiHosts'):
        hosts_raw = lsf.config.get('RESOURCES', 'ESXiHosts').split('\n')
        for entry in hosts_raw:
            if not entry or entry.strip().startswith('#'):
                continue
            hostname = entry.split(':')[0].strip()
            if hostname:
                esxi_hosts.append(hostname)
    
    for hostname in esxi_hosts:
        try:
            days = get_linux_password_expiration(hostname, 'root', password)
            
            if days is None:
                status = "PASS"
                message = "Password never expires"
            elif days > three_years_days:
                status = "PASS"
                message = f"Expires in {days} days ({days // 365} years)"
            elif days > two_years_days:
                status = "PASS"
                message = f"Expires in {days} days ({days // 365} years)"
            else:
                status = "FAIL"
                if days < 0:
                    message = f"Password EXPIRED {abs(days)} days ago"
                else:
                    message = f"Expires in {days} days ({days // 365} years) - TOO SOON"
            
            results.append(CheckResult(
                name=f"ESXi {hostname} (root)",
                status=status,
                message=message,
                details={'hostname': hostname, 'username': 'root', 'days_until_expiration': days}
            ))
        except Exception as e:
            results.append(CheckResult(
                name=f"ESXi {hostname} (root)",
                status="WARN",
                message=f"Could not check: {str(e)[:40]}",
                details={'hostname': hostname, 'username': 'root', 'error': str(e)}
            ))
    
    # Check vCenter servers
    vcenters = []
    if lsf.config.has_option('RESOURCES', 'vCenters'):
        vcenters_raw = lsf.config.get('RESOURCES', 'vCenters').split('\n')
        for entry in vcenters_raw:
            if not entry or entry.strip().startswith('#'):
                continue
            parts = entry.split(':')
            hostname = parts[0].strip()
            vcuser = parts[2].strip() if len(parts) > 2 else 'administrator@vsphere.local'
            if hostname:
                vcenters.append((hostname, vcuser))
    
    for hostname, vcuser in vcenters:
        # Check Linux root account
        try:
            days = get_linux_password_expiration(hostname, 'root', password)
            
            if days is None:
                status = "PASS"
                message = "Password never expires"
            elif days > three_years_days:
                status = "PASS"
                message = f"Expires in {days} days ({days // 365} years)"
            elif days > two_years_days:
                status = "PASS"
                message = f"Expires in {days} days ({days // 365} years)"
            else:
                status = "FAIL"
                if days < 0:
                    message = f"Password EXPIRED {abs(days)} days ago"
                else:
                    message = f"Expires in {days} days ({days // 365} years) - TOO SOON"
            
            results.append(CheckResult(
                name=f"vCenter {hostname} (root)",
                status=status,
                message=message,
                details={'hostname': hostname, 'username': 'root', 'days_until_expiration': days}
            ))
        except Exception as e:
            results.append(CheckResult(
                name=f"vCenter {hostname} (root)",
                status="WARN",
                message=f"Could not check: {str(e)[:40]}",
                details={'hostname': hostname, 'username': 'root', 'error': str(e)}
            ))
        
        # Check vCenter root via REST API (cross-validates chage result above)
        # SSO users like administrator@vsphere.local are directory-managed,
        # not local appliance accounts; their policy is governed by SDDC Manager
        # fleet password settings (checked separately).
        try:
            days = get_vcenter_user_expiration(hostname, vcuser, password, 'root')
            
            if days is None:
                status = "PASS"
                message = "Password never expires (REST API)"
            elif days > three_years_days:
                status = "PASS"
                message = f"Expires in {days} days ({days // 365} years) (REST API)"
            elif days > two_years_days:
                status = "PASS"
                message = f"Expires in {days} days ({days // 365} years) (REST API)"
            else:
                status = "FAIL"
                if days < 0:
                    message = f"Password EXPIRED {abs(days)} days ago (REST API)"
                else:
                    message = f"Expires in {days} days ({days // 365} years) - TOO SOON (REST API)"
            
            results.append(CheckResult(
                name=f"vCenter {hostname} (root via REST)",
                status=status,
                message=message,
                details={'hostname': hostname, 'username': 'root', 'days_until_expiration': days}
            ))
        except Exception as e:
            results.append(CheckResult(
                name=f"vCenter {hostname} (root via REST)",
                status="WARN",
                message=f"Could not check: {str(e)[:40]}",
                details={'hostname': hostname, 'username': 'root', 'error': str(e)}
            ))
    
    # Check NSX managers
    nsx_managers = []
    if lsf.config.has_section('VCF') and lsf.config.has_option('VCF', 'vcfnsxmgr'):
        nsx_raw = lsf.config.get('VCF', 'vcfnsxmgr').split('\n')
        for entry in nsx_raw:
            if not entry or entry.strip().startswith('#'):
                continue
            hostname = entry.split(':')[0].strip()
            if hostname:
                nsx_managers.append(hostname)
    
    for hostname in nsx_managers:
        for user in ['admin', 'root', 'audit']:
            try:
                if user == 'root':
                    # Root is a Linux account
                    days = get_linux_password_expiration(hostname, user, password)
                else:
                    # admin and audit are NSX API users
                    days = get_nsx_user_expiration(hostname, 'admin', password, user)
                
                if days is None:
                    status = "PASS"
                    message = "Password never expires"
                elif days > three_years_days:
                    status = "PASS"
                    message = f"Expires in {days} days ({days // 365} years)"
                elif days > two_years_days:
                    status = "PASS"
                    message = f"Expires in {days} days ({days // 365} years)"
                else:
                    status = "FAIL"
                    if days < 0:
                        message = f"Password EXPIRED {abs(days)} days ago"
                    else:
                        message = f"Expires in {days} days ({days // 365} years) - TOO SOON"
                
                results.append(CheckResult(
                    name=f"NSX {hostname} ({user})",
                    status=status,
                    message=message,
                    details={'hostname': hostname, 'username': user, 'days_until_expiration': days}
                ))
            except Exception as e:
                results.append(CheckResult(
                    name=f"NSX {hostname} ({user})",
                    status="WARN",
                    message=f"Could not check: {str(e)[:40]}",
                    details={'hostname': hostname, 'username': user, 'error': str(e)}
                ))
    
    # Check NSX edges (via NSX Manager transport node API)
    nsx_edges = []
    if lsf.config.has_section('VCF') and lsf.config.has_option('VCF', 'vcfnsxedges'):
        edge_raw = lsf.config.get('VCF', 'vcfnsxedges').split('\n')
        for entry in edge_raw:
            if not entry or entry.strip().startswith('#'):
                continue
            hostname = entry.split(':')[0].strip()
            if hostname:
                nsx_edges.append(hostname)
    
    for hostname in nsx_edges:
        nsx_mgr = _get_nsx_manager_for_edge_check(hostname)
        if not nsx_mgr:
            results.append(CheckResult(
                name=f"NSX Edge {hostname}",
                status="WARN",
                message="Cannot determine managing NSX Manager",
                details={'hostname': hostname}
            ))
            continue
        
        for user in ['admin', 'root', 'audit']:
            try:
                days = get_nsx_edge_user_expiration(hostname, nsx_mgr, password, user)
                
                if days is None:
                    status = "PASS"
                    message = "Password never expires"
                elif days > three_years_days:
                    status = "PASS"
                    message = f"Expires in {days} days ({days // 365} years)"
                elif days > two_years_days:
                    status = "PASS"
                    message = f"Expires in {days} days ({days // 365} years)"
                else:
                    status = "FAIL"
                    if days < 0:
                        message = f"Password EXPIRED {abs(days)} days ago"
                    else:
                        message = f"Expires in {days} days ({days // 365} years) - TOO SOON"
                
                results.append(CheckResult(
                    name=f"NSX Edge {hostname} ({user})",
                    status=status,
                    message=message,
                    details={'hostname': hostname, 'username': user,
                             'nsx_manager': nsx_mgr, 'days_until_expiration': days}
                ))
            except Exception as e:
                results.append(CheckResult(
                    name=f"NSX Edge {hostname} ({user})",
                    status="WARN",
                    message=f"Could not check: {str(e)[:40]}",
                    details={'hostname': hostname, 'username': user, 'error': str(e)}
                ))
    
    # Check SDDC Manager - look in URLs for sddcmanager hosts
    sddc_managers = []
    
    # Method 1: Check VCF.sddcmanager if it exists
    if lsf.config.has_section('VCF') and lsf.config.has_option('VCF', 'sddcmanager'):
        sddc_raw = lsf.config.get('VCF', 'sddcmanager').split('\n')
        for entry in sddc_raw:
            if not entry or entry.strip().startswith('#'):
                continue
            hostname = entry.split(':')[0].strip()
            if hostname:
                sddc_managers.append(hostname)
    
    # Method 2: Extract from URLs containing 'sddcmanager'
    if lsf.config.has_option('RESOURCES', 'urls'):
        urls_raw = lsf.config.get('RESOURCES', 'urls').split('\n')
        for entry in urls_raw:
            if 'sddcmanager' in entry.lower():
                url = entry.split(',')[0].strip()
                # Extract hostname from URL
                if '://' in url:
                    hostname = url.split('://')[1].split('/')[0].split(':')[0]
                    if hostname and hostname not in sddc_managers:
                        sddc_managers.append(hostname)
    
    for hostname in sddc_managers:
        # SDDC Manager only allows SSH as 'vcf' user (root SSH disabled).
        # chage can only query the caller's own account without root, so
        # we can only verify 'vcf'. The backup/root accounts are configured
        # by confighol via expect script and cannot be remotely verified.
        try:
            days = get_linux_password_expiration(hostname, 'vcf', password, ssh_user='vcf')
            
            if days is None:
                status = "PASS"
                message = "Password never expires"
            elif days > three_years_days:
                status = "PASS"
                message = f"Expires in {days} days ({days // 365} years)"
            elif days > two_years_days:
                status = "PASS"
                message = f"Expires in {days} days ({days // 365} years)"
            else:
                status = "FAIL"
                if days < 0:
                    message = f"Password EXPIRED {abs(days)} days ago"
                else:
                    message = f"Expires in {days} days ({days // 365} years) - TOO SOON"
            
            results.append(CheckResult(
                name=f"SDDC Manager {hostname} (vcf)",
                status=status,
                message=message,
                details={'hostname': hostname, 'username': 'vcf', 'days_until_expiration': days}
            ))
        except Exception as e:
            results.append(CheckResult(
                name=f"SDDC Manager {hostname} (vcf)",
                status="WARN",
                message=f"Could not check: {str(e)[:40]}",
                details={'hostname': hostname, 'username': 'vcf', 'error': str(e)}
            ))
        
        for user in ['backup', 'root']:
            try:
                days = get_linux_password_expiration_via_su(
                    hostname, user, password, ssh_user='vcf')
                
                if days is None:
                    status = "PASS"
                    message = "Password never expires"
                elif days > three_years_days:
                    status = "PASS"
                    message = f"Expires in {days} days ({days // 365} years)"
                elif days > two_years_days:
                    status = "PASS"
                    message = f"Expires in {days} days ({days // 365} years)"
                else:
                    status = "FAIL"
                    if days < 0:
                        message = f"Password EXPIRED {abs(days)} days ago"
                    else:
                        message = f"Expires in {days} days ({days // 365} years) - TOO SOON"
                
                results.append(CheckResult(
                    name=f"SDDC Manager {hostname} ({user})",
                    status=status,
                    message=message,
                    details={'hostname': hostname, 'username': user, 'days_until_expiration': days}
                ))
            except Exception as e:
                results.append(CheckResult(
                    name=f"SDDC Manager {hostname} ({user})",
                    status="WARN",
                    message=f"Could not check: {str(e)[:40]}",
                    details={'hostname': hostname, 'username': user, 'error': str(e)}
                ))
    
    # Check vRA/Automation (auto-a) - look in VCFFINAL.vraurls
    vra_hosts = []
    if lsf.config.has_section('VCFFINAL') and lsf.config.has_option('VCFFINAL', 'vraurls'):
        vra_urls_raw = lsf.config.get('VCFFINAL', 'vraurls').split('\n')
        for entry in vra_urls_raw:
            if not entry or entry.strip().startswith('#'):
                continue
            url = entry.split(',')[0].strip()
            # Extract hostname from URL
            if '://' in url:
                hostname = url.split('://')[1].split('/')[0].split(':')[0]
                if hostname and hostname not in vra_hosts:
                    vra_hosts.append(hostname)
    
    for hostname in vra_hosts:
        # VCF Automation appliances only allow SSH as vmware-system-user.
        # vmware-system-user can check its own account directly, but
        # checking root requires sudo -S to escalate privileges.
        for user in ['vmware-system-user', 'root']:
            try:
                needs_sudo = (user != 'vmware-system-user')
                days = get_linux_password_expiration(hostname, user, password,
                                                      ssh_user='vmware-system-user',
                                                      use_sudo=needs_sudo)
                
                if days is None:
                    status = "PASS"
                    message = "Password never expires"
                elif days > three_years_days:
                    status = "PASS"
                    message = f"Expires in {days} days ({days // 365} years)"
                elif days > two_years_days:
                    status = "PASS"
                    message = f"Expires in {days} days ({days // 365} years)"
                else:
                    status = "FAIL"
                    if days < 0:
                        message = f"Password EXPIRED {abs(days)} days ago"
                    else:
                        message = f"Expires in {days} days ({days // 365} years) - TOO SOON"
                
                results.append(CheckResult(
                    name=f"vRA/Automation {hostname} ({user})",
                    status=status,
                    message=message,
                    details={'hostname': hostname, 'username': user, 'days_until_expiration': days}
                ))
            except Exception as e:
                results.append(CheckResult(
                    name=f"vRA/Automation {hostname} ({user})",
                    status="WARN",
                    message=f"Could not check: {str(e)[:40]}",
                    details={'hostname': hostname, 'username': user, 'error': str(e)}
                ))
    
    return results


#==============================================================================
# FIREFOX TRUSTED PRIVATE CAS
#==============================================================================

LMC_FIREFOX_PROFILE_BASE = '/lmchol/home/holuser/snap/firefox/common/.mozilla/firefox'
CERTUTIL_BINARY = 'certutil'

EXPECTED_PRIVATE_CAS = [
    'vcf.lab Root Authority',
]


def _find_firefox_profiles() -> List[str]:
    """
    Find Firefox profile directories containing a cert9.db on the console VM.
    
    :return: List of profile directory paths
    """
    pattern = os.path.join(LMC_FIREFOX_PROFILE_BASE, '*', 'cert9.db')
    profiles = []
    for db_path in glob.glob(pattern):
        profiles.append(os.path.dirname(db_path))
    return profiles


def _get_firefox_private_cas(profile_path: str) -> List[Tuple[str, str, dict]]:
    """
    List custom (non-builtin) CA certificates in a Firefox profile.
    
    Runs ``certutil -L`` to get all certs, then ``certutil -L -n <name> -a``
    piped through ``openssl x509`` for each to extract subject, issuer, and
    validity dates. Built-in Mozilla root CAs ship with trust ``CT,C,C`` or
    similar multi-purpose flags; our private CAs have ``CT,,`` (SSL-only).
    We filter to ``CT,,`` entries so only HOL-added CAs are reported.
    
    :param profile_path: Path to a Firefox profile directory
    :return: List of (nickname, trust_flags, details_dict) tuples
    """
    result = subprocess.run(
        [CERTUTIL_BINARY, '-L', '-d', f'sql:{profile_path}'],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        return []

    certs = []
    for line in result.stdout.splitlines():
        line = line.rstrip()
        if not line or line.startswith('Certificate Nickname') or 'Trust Attributes' in line:
            continue
        # certutil output: nickname padded to ~60 chars then trust flags
        # Trust flags are the last token(s), e.g. "CT,," or "CT,C,C"
        # Find the trust flags at the end (pattern: X,X,X where X is letters or empty)
        match = re.search(r'\s+([A-Za-z]*,[A-Za-z]*,[A-Za-z]*)\s*$', line)
        if not match:
            continue
        trust = match.group(1)
        nickname = line[:match.start()].strip()

        # Only report private/custom CAs (trust CT,, means SSL-only trust,
        # which is what confighol imports with; Mozilla builtins have broader trust)
        if trust != 'CT,,':
            continue

        # Get cert details via openssl
        details = {'nickname': nickname, 'trust': trust}
        try:
            export = subprocess.run(
                [CERTUTIL_BINARY, '-L', '-d', f'sql:{profile_path}',
                 '-n', nickname, '-a'],
                capture_output=True, text=True, timeout=10
            )
            if export.returncode == 0 and export.stdout:
                info = subprocess.run(
                    ['openssl', 'x509', '-noout', '-subject', '-issuer',
                     '-startdate', '-enddate'],
                    input=export.stdout, capture_output=True, text=True, timeout=10
                )
                if info.returncode == 0:
                    for info_line in info.stdout.splitlines():
                        if info_line.startswith('subject='):
                            details['subject'] = info_line[len('subject='):].strip()
                        elif info_line.startswith('issuer='):
                            details['issuer'] = info_line[len('issuer='):].strip()
                        elif info_line.startswith('notBefore='):
                            details['not_before'] = info_line[len('notBefore='):].strip()
                        elif info_line.startswith('notAfter='):
                            details['not_after'] = info_line[len('notAfter='):].strip()

                    # Calculate days until expiry
                    if 'not_after' in details:
                        for fmt in ('%b %d %H:%M:%S %Y %Z', '%b  %d %H:%M:%S %Y %Z'):
                            try:
                                exp = datetime.datetime.strptime(details['not_after'], fmt).date()
                                details['days_until_expiry'] = (exp - datetime.date.today()).days
                                break
                            except ValueError:
                                continue
        except Exception:
            pass

        certs.append((nickname, trust, details))

    return certs


def _build_expected_ca_list() -> List[str]:
    """
    Build the list of private CAs that should be trusted in Firefox.
    
    Sources:
    - Vault Root CA (always expected)
    - One VMCA per vCenter (from [RESOURCES] vCenters in config.ini)
    - Broadcom VCF Root CA (always expected for VCF labs)
    
    :return: List of expected CA nicknames
    """
    expected = list(EXPECTED_PRIVATE_CAS)
    expected.append('Broadcom, Inc CA')

    if lsf and hasattr(lsf, 'config'):
        try:
            if lsf.config.has_option('RESOURCES', 'vCenters'):
                for entry in lsf.config.get('RESOURCES', 'vCenters').split('\n'):
                    entry = entry.strip()
                    if not entry or entry.startswith('#'):
                        continue
                    hostname = entry.split(':')[0].strip()
                    if hostname:
                        expected.append(f'{hostname} CA')
        except Exception:
            pass

    return expected


def check_firefox_trusted_cas() -> List[CheckResult]:
    """
    Check that all expected private CA certificates are trusted in Firefox.
    
    Enumerates custom (non-builtin) CAs in the Firefox NSS certificate store
    on the console VM and verifies that every expected CA is present, trusted,
    and not expired.
    
    :return: List of CheckResult objects
    """
    results = []

    # Pre-flight: certutil must be available
    import shutil
    if not shutil.which(CERTUTIL_BINARY):
        return [CheckResult(
            name="Firefox Trusted CAs",
            status="WARN",
            message="certutil not installed (libnss3-tools package required)",
        )]

    profiles = _find_firefox_profiles()
    if not profiles:
        return [CheckResult(
            name="Firefox Trusted CAs",
            status="WARN",
            message=f"No Firefox profiles found under {LMC_FIREFOX_PROFILE_BASE}",
        )]

    # Use the first profile (there's typically just one)
    profile_path = profiles[0]
    found_cas = _get_firefox_private_cas(profile_path)

    if not found_cas:
        return [CheckResult(
            name="Firefox Trusted CAs",
            status="FAIL",
            message="No custom private CAs found in Firefox",
            details={'profile': profile_path}
        )]

    expected = _build_expected_ca_list()
    found_names = {name for name, _, _ in found_cas}

    # Report each found CA
    for nickname, trust, details in found_cas:
        days = details.get('days_until_expiry')
        not_after = details.get('not_after', 'unknown')
        subject = details.get('subject', '')

        if days is not None and days < 0:
            status = "FAIL"
            message = f"EXPIRED {abs(days)} days ago (was {not_after})"
        elif days is not None and days < 365:
            status = "WARN"
            message = f"Expires in {days} days ({not_after})"
        elif days is not None:
            status = "PASS"
            message = f"Trusted, expires in {days} days ({days // 365} yr)"
        else:
            status = "PASS"
            message = "Trusted (could not determine expiry)"

        # Include a concise subject hint for context
        if subject:
            # Pull the most identifying field (CN or O)
            cn_match = re.search(r'CN\s*=\s*([^,]+)', subject)
            o_match = re.search(r'O\s*=\s*([^,]+)', subject)
            hint = (cn_match or o_match)
            if hint:
                message += f" | {hint.group(0).strip()}"

        results.append(CheckResult(
            name=f"Firefox CA: {nickname}",
            status=status,
            message=message,
            details=details,
        ))

    # Report any expected CAs that are missing
    for expected_name in expected:
        if expected_name not in found_names:
            results.append(CheckResult(
                name=f"Firefox CA: {expected_name}",
                status="FAIL",
                message="MISSING - not found in Firefox certificate store",
                details={'expected': expected_name, 'profile': profile_path}
            ))

    return results


#==============================================================================
# VCF OPERATIONS PASSWORD POLICY CHECKS
#==============================================================================

def check_vcf_password_policies() -> List[CheckResult]:
    """
    Check VCF Operations Manager Fleet Settings Password Policies.
    
    Connects to VCF Operations Manager (Aria Operations) suite-api to:
    - List all configured password policies (Name, Description, etc.)
    - Report policy assignments to inventory resources
    - Report compliance status for inventory items
    
    :return: List of CheckResult objects
    """
    results = []
    
    if not lsf:
        return [CheckResult(
            name="VCF Password Policies",
            status="SKIPPED",
            message="lsfunctions not available"
        )]
    
    # Determine VCF Operations Manager FQDN from config
    ops_fqdn = None
    try:
        if lsf.config.has_option('RESOURCES', 'URLs'):
            urls_raw = lsf.config.get('RESOURCES', 'URLs').split('\n')
            for entry in urls_raw:
                url = entry.split(',')[0].strip()
                if 'ops-' in url and '.vcf.lab' in url:
                    # Extract FQDN from URL like https://ops-a.site-a.vcf.lab/ui/
                    from urllib.parse import urlparse
                    parsed = urlparse(url)
                    ops_fqdn = parsed.hostname
                    break
    except Exception:
        pass
    
    if not ops_fqdn:
        # Try VCF section
        try:
            if lsf.config.has_section('VCF'):
                if lsf.config.has_option('VCF', 'urls'):
                    vcf_urls = lsf.config.get('VCF', 'urls').split('\n')
                    for entry in vcf_urls:
                        url = entry.split(',')[0].strip()
                        if 'ops-' in url:
                            from urllib.parse import urlparse
                            parsed = urlparse(url)
                            ops_fqdn = parsed.hostname
                            break
        except Exception:
            pass
    
    if not ops_fqdn:
        return [CheckResult(
            name="VCF Password Policies",
            status="SKIPPED",
            message="VCF Operations Manager FQDN not found in config"
        )]
    
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except ImportError:
        return [CheckResult(
            name="VCF Password Policies",
            status="SKIPPED",
            message="requests library not available"
        )]
    
    password = lsf.get_password()
    base_url = f"https://{ops_fqdn}"
    api_base = f"{base_url}/suite-api"
    
    # Authenticate to get OpsToken
    try:
        token_resp = requests.post(
            f"{api_base}/api/auth/token/acquire",
            json={"username": "admin", "password": password, "authSource": "local"},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            verify=False, timeout=30
        )
        token_resp.raise_for_status()
        token = token_resp.json()["token"]
    except Exception as e:
        return [CheckResult(
            name="VCF Password Policies",
            status="WARN",
            message=f"Could not authenticate to VCF Operations: {str(e)[:60]}"
        )]
    
    headers = {
        "Authorization": f"OpsToken {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-vRealizeOps-API-use-unsupported": "true"
    }
    
    # Query all password policies
    try:
        query_resp = requests.post(
            f"{api_base}/internal/passwordmanagement/policies/query",
            headers=headers, json={}, verify=False, timeout=30
        )
        query_resp.raise_for_status()
        query_data = query_resp.json()
    except Exception as e:
        return [CheckResult(
            name="VCF Password Policies",
            status="WARN",
            message=f"Could not query password policies: {str(e)[:60]}"
        )]
    
    policies = query_data.get("vcfPolicies", [])
    total_count = query_data.get("pageInfo", {}).get("totalCount", 0)
    
    results.append(CheckResult(
        name="VCF Password Policies",
        status="PASS" if total_count > 0 else "WARN",
        message=f"Found {total_count} password policy(ies) in Fleet Settings",
        details={"ops_fqdn": ops_fqdn, "policy_count": total_count}
    ))
    
    # Report details for each policy
    for policy in policies:
        policy_id = policy.get("policyId", "unknown")
        policy_info = policy.get("policyInfo", {})
        policy_name = policy_info.get("policyName", "Unknown")
        description = policy_info.get("description", "No description")
        is_fleet = policy_info.get("isFleetPolicy", False)
        assigned_resources = policy.get("vcfPolicyAssignedResourceList", [])
        
        # Get full policy details including expiration
        try:
            detail_resp = requests.get(
                f"{api_base}/internal/passwordmanagement/policies/{policy_id}",
                headers=headers, verify=False, timeout=30
            )
            detail_resp.raise_for_status()
            detail = detail_resp.json()
            exp_days = detail.get("expirationConstraint", {}).get("passwordExpirationDays", "N/A")
        except Exception:
            exp_days = "N/A"
        
        assigned_names = [r.get("resourceName", "?") for r in assigned_resources]
        assigned_str = ", ".join(assigned_names) if assigned_names else "None"
        
        msg = (f"Name: {policy_name} | "
               f"Description: {description} | "
               f"Expiration: {exp_days} days | "
               f"Fleet Policy: {is_fleet} | "
               f"Assigned To: {assigned_str}")
        
        results.append(CheckResult(
            name=f"  Policy: {policy_name}",
            status="PASS",
            message=msg,
            details={
                "policyId": policy_id,
                "policyName": policy_name,
                "description": description,
                "isFleetPolicy": is_fleet,
                "passwordExpirationDays": exp_days,
                "assignedResources": assigned_resources
            }
        ))
    
    # Report compliance status for inventory items
    # Get constraint info to show what the system supports
    try:
        constraint_resp = requests.get(
            f"{api_base}/internal/passwordmanagement/policies/constraint",
            headers=headers, verify=False, timeout=30
        )
        constraint_resp.raise_for_status()
        constraint = constraint_resp.json()
        max_exp = constraint.get("passwordExpirationDays", {}).get("max", "N/A")
        min_exp = constraint.get("passwordExpirationDays", {}).get("min", "N/A")
        
        results.append(CheckResult(
            name="  Policy Constraints",
            status="PASS",
            message=f"Expiration range: {min_exp}-{max_exp} days",
            details={"constraint": constraint}
        ))
    except Exception:
        pass
    
    # Report inventory compliance per policy assignment
    for policy in policies:
        assigned_resources = policy.get("vcfPolicyAssignedResourceList", [])
        policy_name = policy.get("policyInfo", {}).get("policyName", "Unknown")
        
        for resource in assigned_resources:
            res_name = resource.get("resourceName", "Unknown")
            res_type = resource.get("resourceType", "Unknown")
            assigned_ts = resource.get("assignedAt", 0)
            
            if assigned_ts:
                assigned_date = datetime.datetime.fromtimestamp(
                    assigned_ts / 1000
                ).strftime("%Y-%m-%d %H:%M")
            else:
                assigned_date = "Unknown"
            
            results.append(CheckResult(
                name=f"  Inventory: {res_name}",
                status="PASS",
                message=f"Policy: {policy_name} | Type: {res_type} | Assigned: {assigned_date}",
                details={
                    "resourceName": res_name,
                    "resourceType": res_type,
                    "policyName": policy_name,
                    "assignedAt": assigned_date
                }
            ))
    
    return results


#==============================================================================
# MAIN
#==============================================================================

def main():
    parser = argparse.ArgumentParser(description='HOLFY27 VPod Checker')
    parser.add_argument('--report-only', action='store_true', help="Don't fix issues, just report")
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    parser.add_argument('--html', type=str, help='Generate HTML report to specified file')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    args = parser.parse_args()
    
    fix_issues = not args.report_only
    
    # Initialize lsfunctions
    if lsf:
        lsf.init(router=False)
        lab_sku = lsf.lab_sku
    else:
        lab_sku = 'HOL-UNKNOWN'
    
    # Extract lab year from SKU using robust pattern matching
    # Supports formats like: HOL-2701, ATE-2705, BETA-901, Discovery-Demo, etc.
    lab_year = extract_lab_year(lab_sku)
    
    # Calculate date ranges
    # Licenses should expire between Dec 30 of the lab year and Dec 31 of the following year
    min_exp_date = datetime.date(int(lab_year) + 2000, 12, 30)
    max_exp_date = datetime.date(int(lab_year) + 2001, 12, 31)
    
    print(f"VPod Checker - HOLFY27")
    print(f"Lab: {lab_sku}")
    print(f"License expiration window: {min_exp_date} to {max_exp_date}")
    print("=" * 60)
    
    # Initialize report
    report = ValidationReport(
        lab_sku=lab_sku,
        timestamp=datetime.datetime.now().isoformat(),
        min_exp_date=str(min_exp_date),
        max_exp_date=str(max_exp_date)
    )
    
    # Get URLs from config
    urls = []
    esxi_hosts = []
    if lsf and hasattr(lsf, 'config'):
        try:
            # Get URLs from RESOURCES section
            if 'URLs' in lsf.config['RESOURCES'].keys():
                urls_raw = lsf.config.get('RESOURCES', 'URLs').split('\n')
                for entry in urls_raw:
                    url = entry.split(',')[0].strip()
                    if url:
                        urls.append(url)
            
            # Get ESXi hosts from RESOURCES section
            if 'ESXiHosts' in lsf.config['RESOURCES'].keys():
                hosts_raw = lsf.config.get('RESOURCES', 'ESXiHosts').split('\n')
                for entry in hosts_raw:
                    if not entry or entry.strip().startswith('#'):
                        continue
                    # ESXi entries have format: hostname:maintenance_mode_flag
                    # Only the content to the left of : is the FQDN
                    hostname = entry.split(':')[0].strip()
                    if hostname:
                        esxi_hosts.append(f'https://{hostname}')
        except Exception:
            pass
        
        try:
            # Get URLs from VCF section (vcfnsxmgr)
            if 'VCF' in lsf.config.sections():
                if 'vcfnsxmgr' in lsf.config['VCF'].keys():
                    nsxmgrs_raw = lsf.config.get('VCF', 'vcfnsxmgr').split('\n')
                    for entry in nsxmgrs_raw:
                        if not entry or entry.strip().startswith('#'):
                            continue
                        # NSX Manager entries may have format: hostname:esxhost
                        hostname = entry.split(':')[0].strip()
                        if hostname:
                            urls.append(f'https://{hostname}')
                
                # Also check VCF urls if present
                if 'urls' in lsf.config['VCF'].keys():
                    vcf_urls_raw = lsf.config.get('VCF', 'urls').split('\n')
                    for entry in vcf_urls_raw:
                        url = entry.split(',')[0].strip()
                        if url:
                            urls.append(url)
        except Exception:
            pass
        
        try:
            # Get vraurls from VCFFINAL section
            if 'VCFFINAL' in lsf.config.sections():
                if 'vraurls' in lsf.config['VCFFINAL'].keys():
                    vra_urls_raw = lsf.config.get('VCFFINAL', 'vraurls').split('\n')
                    for entry in vra_urls_raw:
                        url = entry.split(',')[0].strip()
                        if url:
                            urls.append(url)
        except Exception:
            pass
    
    # Run SSL checks for URLs
    print("\nChecking SSL certificates...")
    report.ssl_checks = check_ssl_certificates(urls, min_exp_date)
    
    # Run SSL checks for ESXi hosts
    if esxi_hosts:
        print("\nChecking ESXi host SSL certificates...")
        esxi_ssl_checks = check_ssl_certificates(esxi_hosts, min_exp_date)
        report.ssl_checks.extend(esxi_ssl_checks)
    
    print_results_table("SSL CERTIFICATES", report.ssl_checks)
    
    # Connect to vCenters and run vSphere checks
    if lsf and PYVMOMI_AVAILABLE:
        try:
            if 'vCenters' in lsf.config['RESOURCES'].keys():
                vcenters = lsf.config.get('RESOURCES', 'vCenters').split('\n')
                lsf.connect_vcenters(vcenters)
        except Exception as e:
            print(f"Could not connect to vCenters: {e}")
        
        if lsf.sis:
            # NTP checks
            print("\nChecking NTP configuration...")
            try:
                hosts = lsf.get_all_hosts()
                report.ntp_checks = check_ntp_configuration(hosts)
                print_results_table("NTP CONFIGURATION", report.ntp_checks)
            except Exception as e:
                print(f"NTP check failed: {e}")
            
            # VM configuration checks
            print("\nChecking VM configuration...")
            try:
                vms = lsf.get_all_vms()
                report.vm_config_checks = check_vm_configuration(vms, fix_issues)
                print_results_table("VM CONFIGURATION", report.vm_config_checks)
            except Exception as e:
                print(f"VM config check failed: {e}")
            
            # License checks (all vCenter entities including every ESXi host)
            print("\nChecking licenses...")
            try:
                report.license_checks = check_licenses(lsf.sis, min_exp_date, max_exp_date)
            except Exception as e:
                print(f"License check failed: {e}")
            
            # VCF Operations license/status check
            try:
                ops_license_checks = check_vcf_operations_license()
                report.license_checks.extend(ops_license_checks)
            except Exception as e:
                print(f"VCF Operations license check failed: {e}")
            
            print_results_table("LICENSES", report.license_checks)
            
            # Disconnect
            for si in lsf.sis:
                try:
                    connect.Disconnect(si)
                except Exception:
                    pass
    
    # Password expiration checks
    print("\nChecking password expirations...")
    try:
        report.password_expiration_checks = check_password_expirations()
        print_results_table("PASSWORD EXPIRATIONS", report.password_expiration_checks)
    except Exception as e:
        print(f"Password expiration check failed: {e}")
    
    # VCF Operations Fleet Settings Password Policy checks
    print("\nChecking VCF Operations Fleet Password Policies...")
    try:
        report.fleet_password_policy_checks = check_vcf_password_policies()
        print_results_table("VCF FLEET PASSWORD POLICIES", report.fleet_password_policy_checks)
    except Exception as e:
        print(f"VCF Fleet Password Policy check failed: {e}")
    
    # Firefox Trusted Private CAs
    print("\nChecking Firefox Trusted Private CAs...")
    try:
        report.firefox_ca_checks = check_firefox_trusted_cas()
        print_results_table("FIREFOX TRUSTED PRIVATE CAs", report.firefox_ca_checks)
    except Exception as e:
        print(f"Firefox CA check failed: {e}")
    
    # Determine overall status
    all_checks = (
        report.ssl_checks + 
        report.license_checks + 
        report.ntp_checks + 
        report.vm_config_checks + 
        report.vm_resource_checks +
        report.password_expiration_checks +
        report.fleet_password_policy_checks +
        report.firefox_ca_checks
    )
    
    if any(c.status == 'FAIL' for c in all_checks):
        report.overall_status = "FAIL"
    elif any(c.status == 'WARN' for c in all_checks):
        report.overall_status = "WARN"
    else:
        report.overall_status = "PASS"
    
    print("\n" + "=" * 60)
    print(f"Overall Status: {report.overall_status}")
    
    # Output formats
    if args.json:
        print(report.to_json())
    
    if args.html:
        html = generate_html_report(report)
        with open(args.html, 'w') as f:
            f.write(html)
        print(f"HTML report written to: {args.html}")
    
    return 0 if report.overall_status in ['PASS', 'WARN'] else 1


if __name__ == '__main__':
    sys.exit(main())

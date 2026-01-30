#!/usr/bin/env python3
# vpodchecker.py - HOLFY27 Lab Validation Tool
# Version 2.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Modernized for HOLFY27 architecture with enhanced checks and reporting

"""
VPod Checker - Validates lab configuration against HOL standards

This tool checks:
- SSL certificate expiration dates
- vSphere license validity and expiration
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
from typing import Dict, List, Optional, Any
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
    overall_status: str = "PASS"
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)


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
            host.days_to_expire = (host.ssl_exp_date - min_exp_date).days
            
            if host.days_to_expire < 0:
                status = "FAIL"
                message = f"Certificate expires before lab expiration! ({host.ssl_exp_date})"
            elif host.days_to_expire < 30:
                status = "WARN"
                message = f"Certificate expires soon ({host.ssl_exp_date})"
            else:
                status = "PASS"
                message = f"Certificate valid until {host.ssl_exp_date}"
            
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
            results.append(CheckResult(
                name=f"SSL: {host.name}:{host.port}",
                status="FAIL",
                message=f"Could not check certificate: {e}",
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
        'vCLS-',                              # vSphere Cluster Services VMs
        'vcf-services-platform-template-',    # VCF Services Platform Template VMs
        'SupervisorControlPlaneVM',           # Tanzu Supervisor Control Plane VMs
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
    """Check vSphere licenses"""
    results = []
    license_keys_checked = set()
    
    for si in sis:
        try:
            lic_mgr = si.content.licenseManager
            lic_assignment_mgr = lic_mgr.licenseAssignmentManager
            assets = lic_assignment_mgr.QueryAssignedLicenses()
            
            for asset in assets:
                license_key = asset.assignedLicense.licenseKey
                license_name = asset.assignedLicense.name
                entity_name = asset.entityDisplayName
                
                if license_key in license_keys_checked:
                    continue
                license_keys_checked.add(license_key)
                
                # Check for evaluation license
                if license_key == '00000-00000-00000-00000-00000':
                    results.append(CheckResult(
                        name=f"License: {entity_name}",
                        status="FAIL",
                        message="Evaluation license detected - no expiration date",
                        details={"license_key": license_key, "entity": entity_name}
                    ))
                    continue
                
                # Get expiration date
                exp_date = None
                for prop in asset.assignedLicense.properties:
                    if prop.key == 'expirationDate':
                        exp_date = prop.value
                        break
                
                if exp_date:
                    # Use the new expiration status logic based on months from today
                    status, message = get_license_expiration_status(exp_date.date())
                else:
                    if 'NSX for vShield Endpoint' in license_name:
                        status = "WARN"
                        message = "Non-expiring license (expected for vShield Endpoint) - no expiration date"
                    else:
                        status = "FAIL"
                        message = "Non-expiring license detected - no expiration date"
                
                results.append(CheckResult(
                    name=f"License: {license_name}",
                    status=status,
                    message=message,
                    details={
                        "license_key": license_key[:5] + "-****-****-****-" + license_key[-5:],
                        "entity": entity_name,
                        "expiration": str(exp_date.date()) if exp_date else "Never"
                    }
                ))
            
            # Check for unassigned licenses
            for lic in lic_mgr.licenses:
                if not lic.used and lic.licenseKey != '00000-00000-00000-00000-00000':
                    # Get expiration date for unassigned license
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
        ('VM Resources', report.vm_resource_checks)
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
        lab_year = lsf.lab_sku[4:6] if len(lsf.lab_sku) >= 6 else '27'
    else:
        lab_sku = 'HOL-UNKNOWN'
        lab_year = '27'
    
    # Calculate date ranges
    min_exp_date = datetime.date(int(lab_year) + 2000, 12, 30)
    max_exp_date = datetime.date(int(lab_year) + 2001, 1, 31)
    
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
            
            # License checks
            print("\nChecking licenses...")
            try:
                report.license_checks = check_licenses(lsf.sis, min_exp_date, max_exp_date)
                print_results_table("LICENSES", report.license_checks)
            except Exception as e:
                print(f"License check failed: {e}")
            
            # Disconnect
            for si in lsf.sis:
                try:
                    connect.Disconnect(si)
                except Exception:
                    pass
    
    # Determine overall status
    all_checks = (
        report.ssl_checks + 
        report.license_checks + 
        report.ntp_checks + 
        report.vm_config_checks + 
        report.vm_resource_checks
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

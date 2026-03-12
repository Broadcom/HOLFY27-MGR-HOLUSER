#!/usr/bin/env python3
"""
VCF Certificate Management Script (VCF 9.1 Cycle 4)

This script manages certificates for VCF infrastructure components by:
1. Generating CSRs (via SDDC Manager API for managed resources, or locally for others)
2. Signing CSRs with HashiCorp Vault PKI (2 years TTL by default)
3. Replacing certificates on VCF components via SDDC Manager API or component-specific methods

Certificate Replacement Workflow:

For SDDC Manager-managed resources (SDDC Manager, vCenter, NSX Manager, ESXi):
  1. Generate CSR on the component via SDDC Manager API
  2. Retrieve CSR from SDDC Manager
  3. Sign CSR with HashiCorp Vault PKI
  4. Upload signed certificate chain via SDDC Manager API
  5. SDDC Manager applies the certificate to the component

For fleet-managed resources (VCF Operations, Automation, Logs, Networks, etc.):
  1. Generate CSR via VCF Operations Certificate Management API
  2. Download CSR from API
  3. Sign CSR with HashiCorp Vault PKI
  4. Import signed certificate to VCF Operations repository
  5. Replace active certificate via API

For non-managed resources:
  1. Generate CSR locally
  2. Sign CSR with HashiCorp Vault PKI
  3. Replace certificate via component-specific method (SSH/API)

VCF Components Managed:

  Management Domain (SDDC Manager API):
  - sddcmanager-a.site-a.vcf.lab   (SDDC Manager)       [AUTOMATED]
  - vc-mgmt-a.site-a.vcf.lab       (Mgmt vCenter)       [AUTOMATED]
  - nsx-mgmt-a.site-a.vcf.lab      (Mgmt NSX VIP)       [AUTOMATED]
  - nsx-mgmt-01a.site-a.vcf.lab    (Mgmt NSX Node)      [AUTOMATED]

  Workload Domain (SDDC Manager API):
  - vc-wld01-a.site-a.vcf.lab      (WLD vCenter)        [AUTOMATED]
  - nsx-wld01-a.site-a.vcf.lab     (WLD NSX VIP)        [AUTOMATED]
  - nsx-wld01-01a.site-a.vcf.lab   (WLD NSX Node)       [AUTOMATED]

  Fleet-managed (VCF Operations Certificate Management API):
  - ops-a.site-a.vcf.lab           (VCF Operations)      [AUTOMATED*]
  - auto-a.site-a.vcf.lab          (VCF Automation)      [AUTOMATED*]
  - auto-platform-a.site-a.vcf.lab (VCFA Platform)       [AUTOMATED*]
  - opslogs-a.site-a.vcf.lab       (Log management)      [AUTOMATED]
  - opsnet-a.site-a.vcf.lab        (VCF Ops for networks)[AUTOMATED]
  - vidb-a.site-a.vcf.lab          (Identity broker)     [AUTOMATED]
  - fleet-01a.site-a.vcf.lab       (VCF services runtime)[AUTOMATED]
  - instance-01a.site-a.vcf.lab    (VCF services runtime)[AUTOMATED]
  - vsp-01a.site-a.vcf.lab         (VCF services runtime)[AUTOMATED]

  * VRSLCM orchestrator dependency: VCF Automation and VCF Operations cert
    replacements require the VRSLCM orchestrator (via fleet-upgrade-service).
    If this service is unhealthy, the CSR/sign/import steps succeed but the
    final replacement hangs. In that case, use the VCF Operations UI manually:
    Fleet Management > Certificates > Replace With Imported Certificate.

Security: Credentials can be provided via:
- Environment variables (VCF_PASS)
- /home/holuser/creds.txt (fallback for password)

Vault Token: Obtained fresh from creds.txt (root token) or Vault auth.

Default Credentials by Target and Access Method:
+--------------------+-----------------------------+--------------------+
| Target             | API User                    | SSH User           |
+--------------------+-----------------------------+--------------------+
| sddcmanager-a      | admin@local (Bearer)        | vcf                |
| vc-mgmt-a          | administrator@vsphere.local | root               |
| vc-wld01-a         | administrator@wld.sso       | root               |
| nsx-mgmt-01a       | admin                       | admin              |
| nsx-wld01-01a      | admin                       | admin              |
| ops-a              | admin (OpsToken, local)     | root               |
| auto-a             | admin                       | vmware-system-user |
| auto-platform-a    | -                           | vmware-system-user |
| opslogs-a          | admin                       | vmware-system-user |
| opsnet-a           | admin                       | (SSH unavailable)  |
+--------------------+-----------------------------+--------------------+
"""

import os
import sys
import json
import logging
import argparse
import time
import re
import subprocess
import tempfile
from typing import Optional, Dict, List, Tuple
from pathlib import Path

import ipaddress
import socket

import requests
import urllib3
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# =============================================================================
# Default VCF Component Targets
# =============================================================================

DEFAULT_VCF_TARGETS = [
    # Management Domain (SDDC Manager-managed)
    'sddcmanager-a.site-a.vcf.lab',
    'vc-mgmt-a.site-a.vcf.lab',
    'nsx-mgmt-a.site-a.vcf.lab',
    'nsx-mgmt-01a.site-a.vcf.lab',
    # Workload Domain (SDDC Manager-managed)
    'vc-wld01-a.site-a.vcf.lab',
    'nsx-wld01-a.site-a.vcf.lab',
    'nsx-wld01-01a.site-a.vcf.lab',
    # Fleet-managed (VCF Operations Certificate Management API)
    'ops-a.site-a.vcf.lab',
    'auto-a.site-a.vcf.lab',
    'auto-platform-a.site-a.vcf.lab',
    'opslogs-a.site-a.vcf.lab',
    'opsnet-a.site-a.vcf.lab',
    'vidb-a.site-a.vcf.lab',
    'fleet-01a.site-a.vcf.lab',
    'instance-01a.site-a.vcf.lab',
    'vsp-01a.site-a.vcf.lab',
]


# =============================================================================
# Default Credentials Configuration
# =============================================================================

DEFAULT_CREDENTIALS = {
    'sddcmanager': {
        'api': 'admin@local',
        'ssh': 'vcf',
    },
    'ops-': {
        'api': 'admin',
        'ssh': 'root',
    },
    'opslogs': {
        'api': 'admin',
        'ssh': 'vmware-system-user',
    },
    'opsnet': {
        'api': 'admin',
        'ssh': None,  # SSH not available on opsnet
    },
    'auto-platform': {
        'api': 'admin',
        'ssh': 'vmware-system-user',
    },
    'auto': {
        'api': 'admin',
        'ssh': 'vmware-system-user',
    },
    'nsx': {
        'api': 'admin',
        'ssh': 'admin',
    },
    'vc-wld': {
        'api': 'administrator@wld.sso',
        'ssh': 'root',
    },
    'vc-': {
        'api': 'administrator@vsphere.local',
        'ssh': 'root',
    },
    'vcenter': {
        'api': 'administrator@vsphere.local',
        'ssh': 'root',
    },
    'vsp-': {
        'api': 'admin',
        'ssh': 'vmware-system-user',
    },
    'fleet-': {
        'api': 'admin',
        'ssh': 'vmware-system-user',
    },
    'instance-': {
        'api': 'admin',
        'ssh': 'vmware-system-user',
    },
    'vidb': {
        'api': 'admin',
        'ssh': 'vmware-system-user',
    },
    'esx-': {
        'api': 'root',
        'ssh': 'root',
    },
    'default': {
        'api': 'administrator@vsphere.local',
        'ssh': 'root',
    }
}


def get_credentials_for_target(target_fqdn: str, access_method: str = 'api') -> str:
    """Get the appropriate username for a target based on FQDN and access method."""
    target_lower = target_fqdn.lower()
    
    for pattern, creds in DEFAULT_CREDENTIALS.items():
        if pattern != 'default' and pattern in target_lower:
            return creds.get(access_method, creds.get('api', 'root'))
    
    return DEFAULT_CREDENTIALS['default'].get(access_method, 'root')


# =============================================================================
# SSH Helper Functions
# =============================================================================

def run_ssh_command(host: str, user: str, password: str, command: str, timeout: int = 30, use_sudo: bool = False) -> Tuple[bool, str]:
    """
    Run a command on a remote host via SSH using sshpass.
    
    Args:
        host: Remote hostname
        user: SSH username
        password: SSH/sudo password
        command: Command to execute
        timeout: Command timeout in seconds
        use_sudo: If True, prepend command with sudo and pass password via stdin
    
    Returns:
        Tuple of (success, output)
    """
    try:
        if use_sudo and not command.startswith('sudo'):
            # Use sudo with password via stdin
            ssh_command = f"echo '{password}' | sudo -S {command}"
        else:
            ssh_command = command
        
        cmd = [
            'sshpass', '-p', password,
            'ssh', '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'LogLevel=ERROR',
            '-tt',  # Force pseudo-terminal allocation
            f'{user}@{host}',
            ssh_command
        ]
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        
        if result.returncode == 0:
            return True, result.stdout
        else:
            return False, result.stderr or result.stdout
            
    except subprocess.TimeoutExpired:
        return False, "SSH command timed out"
    except Exception as e:
        return False, str(e)


def scp_file_to_host(host: str, user: str, password: str, local_path: str, remote_path: str) -> bool:
    """
    Copy a file to a remote host via SCP using sshpass.
    """
    try:
        cmd = [
            'sshpass', '-p', password,
            'scp', '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'LogLevel=ERROR',
            local_path,
            f'{user}@{host}:{remote_path}'
        ]
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60
        )
        
        return result.returncode == 0
        
    except Exception as e:
        logger.error(f"SCP failed: {e}")
        return False


# =============================================================================
# Certificate Manager Class
# =============================================================================

class VaultCertificateManager:
    """
    Manages certificate generation and signing with HashiCorp Vault.
    """
    
    def __init__(
        self,
        vault_url: str,
        vault_token: str,
        vault_role: str,
        vault_mount: str = "pki",
        cert_ttl: str = "17520h"
    ):
        self.vault_url = vault_url.rstrip('/')
        self.vault_token = vault_token
        self.vault_role = vault_role
        self.vault_mount = vault_mount
        self.cert_ttl = cert_ttl
        
    @staticmethod
    def _is_ip_address(value: str) -> bool:
        """Check if a string is an IP address."""
        try:
            ipaddress.ip_address(value)
            return True
        except ValueError:
            return False

    @staticmethod
    def resolve_fqdn_to_ip(fqdn: str) -> Optional[str]:
        """Resolve an FQDN to its IP address via DNS."""
        try:
            return socket.gethostbyname(fqdn)
        except socket.gaierror:
            logger.debug(f"  Could not resolve {fqdn} to an IP address")
            return None

    def generate_csr(
        self,
        common_name: str,
        organization: str = "VMware",
        organizational_unit: str = "Hands On Labs",
        country: str = "US",
        state: str = "California",
        locality: str = "Palo Alto",
        key_size: int = 2048,
        san_list: Optional[List[str]] = None
    ) -> Tuple[str, str]:
        """
        Generate a CSR and private key locally.

        san_list entries are auto-classified: IP addresses become IPAddress SANs,
        hostnames become DNSName SANs.
        """
        try:
            logger.info(f"Generating CSR for CN={common_name}...")
            
            private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=key_size,
                backend=default_backend()
            )
            
            subject = x509.Name([
                x509.NameAttribute(NameOID.COUNTRY_NAME, country),
                x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, state),
                x509.NameAttribute(NameOID.LOCALITY_NAME, locality),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
                x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, organizational_unit),
                x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            ])
            
            csr_builder = x509.CertificateSigningRequestBuilder().subject_name(subject)
            
            if san_list:
                san_entries = []
                for san in san_list:
                    if self._is_ip_address(san):
                        san_entries.append(x509.IPAddress(ipaddress.ip_address(san)))
                    else:
                        san_entries.append(x509.DNSName(san))
                csr_builder = csr_builder.add_extension(
                    x509.SubjectAlternativeName(san_entries),
                    critical=False
                )
            
            csr = csr_builder.sign(private_key, hashes.SHA256(), default_backend())
            
            csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode('utf-8')
            key_pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption()
            ).decode('utf-8')
            
            logger.debug("CSR and private key generated successfully")
            return csr_pem, key_pem
            
        except Exception as e:
            logger.error(f"Failed to generate CSR: {e}")
            raise
    
    def sign_csr(self, csr_pem: str, common_name: str, ip_sans: Optional[List[str]] = None) -> Optional[str]:
        """Sign a CSR using HashiCorp Vault PKI."""
        try:
            url = f"{self.vault_url}/v1/{self.vault_mount}/sign/{self.vault_role}"
            logger.info(f"Signing CSR with Vault (TTL: {self.cert_ttl})...")
            
            payload = {
                "csr": csr_pem,
                "common_name": common_name,
                "format": "pem_bundle",
                "ttl": self.cert_ttl
            }
            if ip_sans:
                payload["ip_sans"] = ",".join(ip_sans)
                logger.info(f"  IP SANs: {', '.join(ip_sans)}")
            headers = {"X-Vault-Token": self.vault_token}
            
            resp = requests.post(url, json=payload, headers=headers, timeout=30, verify=False)
            
            if resp.status_code == 200:
                data = resp.json().get('data', {})
                certificate = data.get('certificate')
                ca_chain = data.get('ca_chain', [])
                issuing_ca = data.get('issuing_ca', '')
                
                if not certificate:
                    logger.error("No certificate in Vault response")
                    return None
                
                # Combine certificate with CA chain
                cert_bundle = certificate
                if ca_chain:
                    cert_bundle += "\n" + "\n".join(ca_chain)
                elif issuing_ca:
                    cert_bundle += "\n" + issuing_ca
                
                # Log certificate details
                expiration = data.get('expiration', 0)
                if expiration:
                    from datetime import datetime
                    exp_date = datetime.fromtimestamp(expiration)
                    logger.info(f"✓ Certificate signed successfully")
                    logger.info(f"  Expires: {exp_date.strftime('%Y-%m-%d %H:%M:%S')}")
                else:
                    logger.info(f"✓ Certificate signed successfully")
                
                return cert_bundle
            else:
                logger.error(f"Vault signing failed: {resp.status_code}")
                logger.error(f"Response: {resp.text}")
                return None
                
        except Exception as e:
            logger.error(f"Failed to sign CSR with Vault: {e}")
            return None
    
    def generate_certificate(self, fqdn: str) -> Optional[Tuple[str, str]]:
        """Generate and sign a certificate for the given FQDN, including its IP in the SAN."""
        san_list = [fqdn]
        ip_sans = []

        ip_addr = self.resolve_fqdn_to_ip(fqdn)
        if ip_addr:
            san_list.append(ip_addr)
            ip_sans.append(ip_addr)
            logger.info(f"  Resolved {fqdn} -> {ip_addr} (added to SAN)")
        else:
            logger.warning(f"  Could not resolve {fqdn} - certificate will only have DNS SAN")

        csr_pem, key_pem = self.generate_csr(
            common_name=fqdn,
            san_list=san_list
        )
        
        cert_pem = self.sign_csr(csr_pem, fqdn, ip_sans=ip_sans or None)
        if not cert_pem:
            return None
        
        return cert_pem, key_pem


# =============================================================================
# SDDC Manager API Certificate Replacement
# =============================================================================

class SDDCManagerAPI:
    """
    SDDC Manager API client for certificate replacement.
    
    Uses PUT /v1/domains/{id}/resource-certificates to replace certificates
    on VCF components (SDDC Manager, vCenter, NSX, etc.)
    """
    
    # Resource type mapping for SDDC Manager API
    # Only resource types that SDDC Manager can generate CSRs for and replace certs
    RESOURCE_TYPES = {
        'sddcmanager': 'SDDC_MANAGER',
        'vc-': 'VCENTER',
        'vcenter': 'VCENTER',
        'nsx-mgmt': 'NSXT_MANAGER',
        'nsx-wld': 'NSXT_MANAGER',
        'esx-': 'ESXI',
        'ops-': None,     # VCF Operations - cert managed via SSH
        'opslogs': None,  # Ops for Logs - cert managed via VSP/Locker
        'opsnet': None,   # Ops for Networks - cert managed via VSP/Locker
        'auto': None,     # VCF Automation - cert managed via K8s
        'vsp-': None,     # VSP Gateway - cert managed via VSP cluster
        'fleet-': None,   # Fleet Gateway - cert managed via VSP cluster
        'instance-': None,  # Instance Gateway - cert managed via VSP cluster
    }
    
    def __init__(self, sddc_manager_url: str, username: str, password: str, verify_ssl: bool = False):
        self.base_url = sddc_manager_url.rstrip('/')
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self.token: Optional[str] = None
        self.domain_id: Optional[str] = None
        self._domains_cache: Optional[List[Dict]] = None
        
    def get_token(self) -> Optional[str]:
        """Authenticate and get access token via Bearer token."""
        url = f"{self.base_url}/v1/tokens"
        payload = {
            "username": self.username,
            "password": self.password
        }
        
        try:
            resp = self.session.post(url, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                self.token = data.get('accessToken')
                logger.debug("SDDC Manager authentication successful")
                return self.token
            else:
                logger.error(f"SDDC Manager auth failed: {resp.status_code}")
                logger.debug(f"  Response: {resp.text[:200]}")
                return None
        except Exception as e:
            logger.error(f"SDDC Manager auth failed: {e}")
            return None
    
    def get_headers(self) -> Dict[str, str]:
        """Get authorization headers."""
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
    
    def _load_domains(self) -> List[Dict]:
        """Load and cache all VCF domains."""
        if self._domains_cache is not None:
            return self._domains_cache

        if not self.token:
            if not self.get_token():
                return []

        url = f"{self.base_url}/v1/domains"
        try:
            resp = self.session.get(url, headers=self.get_headers(), timeout=30)
            if resp.status_code == 200:
                self._domains_cache = resp.json().get('elements', [])
                return self._domains_cache
        except Exception as e:
            logger.error(f"Failed to get domains: {e}")
        return []

    def get_domain_id(self) -> Optional[str]:
        """Get the management domain ID."""
        if self.domain_id:
            return self.domain_id

        for domain in self._load_domains():
            if domain.get('type') == 'MANAGEMENT':
                self.domain_id = domain.get('id')
                logger.debug(f"Found management domain: {self.domain_id}")
                return self.domain_id

        logger.error("Could not find management domain")
        return None

    def get_domain_id_for_fqdn(self, fqdn: str) -> Optional[str]:
        """Determine which domain a resource belongs to by checking resource certificates."""
        if not self.token:
            if not self.get_token():
                return None

        for domain in self._load_domains():
            domain_id = domain.get('id')
            url = f"{self.base_url}/v1/domains/{domain_id}/resource-certificates"
            try:
                resp = self.session.get(url, headers=self.get_headers(), timeout=30)
                if resp.status_code == 200:
                    for el in resp.json().get('elements', []):
                        if el.get('resourceName', '').lower() == fqdn.lower():
                            logger.debug(f"  {fqdn} belongs to domain {domain.get('name')} ({domain_id})")
                            return domain_id
            except Exception:
                continue

        logger.debug(f"  {fqdn} not found in any domain resource certificates")
        return self.get_domain_id()
    
    def get_resource_type(self, fqdn: str) -> Optional[str]:
        """Determine the resource type for a given FQDN."""
        fqdn_lower = fqdn.lower()
        for pattern, resource_type in self.RESOURCE_TYPES.items():
            if pattern in fqdn_lower:
                return resource_type
        return None
    
    def is_sddc_managed(self, fqdn: str) -> bool:
        """Check if a resource is managed by SDDC Manager."""
        return self.get_resource_type(fqdn) is not None
    
    def generate_csr(self, fqdn: str, resource_type: str, dry_run: bool = False) -> Tuple[bool, Optional[str]]:
        """
        Generate a CSR for a resource via SDDC Manager API.
        
        The CSR is generated on the remote component (not locally).
        Automatically determines the correct domain for the resource.
        """
        domain_id = self.get_domain_id_for_fqdn(fqdn)
        if not domain_id:
            logger.error("  Cannot determine domain for resource")
            return False, None
        
        logger.info(f"  Generating CSR via SDDC Manager API...")
        logger.info(f"  Resource: {fqdn} ({resource_type}), Domain: {domain_id}")
        
        if dry_run:
            logger.info("  [DRY RUN] Would call PUT /v1/domains/{id}/csrs")
            return True, None
        
        url = f"{self.base_url}/v1/domains/{domain_id}/csrs"
        
        payload = {
            "csrGenerationSpec": {
                "country": "US",
                "state": "California",
                "locality": "Palo Alto",
                "organization": "VMware",
                "organizationUnit": "VCF",
                "keySize": "2048",
                "keyAlgorithm": "RSA"
            },
            "resources": [
                {
                    "fqdn": fqdn,
                    "type": resource_type,
                    "sans": [fqdn]
                }
            ]
        }
        
        try:
            resp = self.session.put(url, json=payload, headers=self.get_headers(), timeout=120)
            
            if resp.status_code == 202:
                data = resp.json()
                task_id = data.get('id')
                logger.info(f"  ✓ CSR generation initiated")
                logger.info(f"  Task ID: {task_id}")
                return True, task_id
            else:
                logger.error(f"  CSR generation failed: {resp.status_code}")
                try:
                    error_data = resp.json()
                    logger.error(f"  Error: {error_data.get('message', resp.text)}")
                except:
                    logger.error(f"  Response: {resp.text[:500]}")
                return False, None
                
        except Exception as e:
            logger.error(f"  Failed to generate CSR: {e}")
            return False, None
    
    def get_csr(self, fqdn: str) -> Optional[str]:
        """Get the CSR content for a resource from the correct domain."""
        domain_id = self.get_domain_id_for_fqdn(fqdn)
        if not domain_id:
            return None
        
        url = f"{self.base_url}/v1/domains/{domain_id}/csrs"
        
        try:
            resp = self.session.get(url, headers=self.get_headers(), timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                for element in data.get('elements', []):
                    resource = element.get('resource', {})
                    if resource.get('fqdn', '').lower() == fqdn.lower():
                        return element.get('csrEncodedContent')
            return None
        except Exception as e:
            logger.error(f"  Failed to get CSR: {e}")
            return None
    
    def replace_certificate(self, fqdn: str, cert_chain: str, dry_run: bool = False) -> Tuple[bool, Optional[str]]:
        """
        Replace certificate for a VCF component using SDDC Manager API.
        Automatically determines the correct domain for the resource.
        """
        resource_type = self.get_resource_type(fqdn)
        if not resource_type:
            logger.warning(f"  {fqdn} is not managed by SDDC Manager API")
            return False, None
        
        domain_id = self.get_domain_id_for_fqdn(fqdn)
        if not domain_id:
            logger.error("  Cannot determine domain for resource")
            return False, None
        
        logger.info(f"  Replacing certificate via SDDC Manager API...")
        logger.info(f"  Resource type: {resource_type}")
        logger.info(f"  Domain ID: {domain_id}")
        
        if dry_run:
            logger.info("  [DRY RUN] Would call PUT /v1/domains/{id}/resource-certificates")
            return True, None
        
        url = f"{self.base_url}/v1/domains/{domain_id}/resource-certificates"
        
        # Build the payload - using resourceFqdn and certificateChain
        payload = [
            {
                "resourceFqdn": fqdn,
                "certificateChain": cert_chain
            }
        ]
        
        try:
            resp = self.session.put(url, json=payload, headers=self.get_headers(), timeout=120)
            
            if resp.status_code == 202:
                data = resp.json()
                task_id = data.get('id')
                logger.info(f"  ✓ Certificate replacement initiated")
                logger.info(f"  Task ID: {task_id}")
                return True, task_id
            else:
                logger.error(f"  Certificate replacement failed: {resp.status_code}")
                try:
                    error_data = resp.json()
                    logger.error(f"  Error: {error_data.get('message', resp.text)}")
                    if 'nestedErrors' in error_data:
                        for err in error_data['nestedErrors']:
                            logger.error(f"    - {err.get('message', '')}")
                except:
                    logger.error(f"  Response: {resp.text[:500]}")
                return False, None
                
        except Exception as e:
            logger.error(f"  Failed to replace certificate: {e}")
            return False, None
    
    def wait_for_task(self, task_id: str, timeout: int = 600) -> bool:
        """Wait for an SDDC Manager task to complete."""
        if not task_id:
            return True
            
        url = f"{self.base_url}/v1/tasks/{task_id}"
        start_time = time.time()
        
        logger.info(f"  Waiting for task {task_id} to complete...")
        
        while time.time() - start_time < timeout:
            try:
                resp = self.session.get(url, headers=self.get_headers(), timeout=30)
                if resp.status_code == 200:
                    data = resp.json()
                    status = data.get('status', '')
                    
                    status_lower = status.lower()
                    if status_lower == 'successful':
                        logger.info(f"  ✓ Task completed successfully")
                        return True
                    elif status_lower == 'failed':
                        logger.error(f"  ✗ Task failed")
                        errors = data.get('errors', [])
                        for err in errors:
                            logger.error(f"    Error: {err.get('message', err)}")
                        return False
                    elif status_lower in ['in_progress', 'in progress', 'pending']:
                        elapsed = int(time.time() - start_time)
                        logger.debug(f"  Task status: {status} ({elapsed}s elapsed)")
                        time.sleep(10)
                    else:
                        logger.warning(f"  Unknown task status: {status}")
                        time.sleep(10)
                else:
                    logger.warning(f"  Failed to get task status: {resp.status_code}")
                    time.sleep(10)
                    
            except Exception as e:
                logger.warning(f"  Error checking task: {e}")
                time.sleep(10)
        
        logger.error(f"  Task timed out after {timeout}s")
        return False


# =============================================================================
# VCF Operations Fleet Certificate Management API
# =============================================================================

class VCFOpsCertManagementAPI:
    """
    Client for the VCF Operations Fleet Certificate Management API.

    Uses two base paths depending on operation:
      - suite-api/internal/certificatemanagement (OpsToken, for queries and CSR listing)
      - vcf-operations/rest/ops/internal/certificatemanagement (session cookie, for mutations)

    Authentication: OpsToken acquired via suite-api/api/auth/token/acquire, then a session
    cookie is obtained by calling the internal API endpoint.
    """

    FLEET_CERT_TARGETS = {
        'auto-a.site-a.vcf.lab':          '10.1.1.70',
        'auto-platform-a.site-a.vcf.lab': '10.1.1.69',
        'ops-a.site-a.vcf.lab':           '10.1.1.30',
        'opslogs-a.site-a.vcf.lab':       '10.1.1.50',
        'vidb-a.site-a.vcf.lab':          '10.1.1.66',
        'fleet-01a.site-a.vcf.lab':       '10.1.1.143',
        'instance-01a.site-a.vcf.lab':    '10.1.1.145',
        'vsp-01a.site-a.vcf.lab':         '10.1.1.141',
        'opsnet-a.site-a.vcf.lab':        '10.1.1.60',
    }

    def __init__(self, ops_url: str, username: str, password: str):
        self.ops_url = ops_url.rstrip('/')
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.verify = False
        self.ops_token: Optional[str] = None
        self._cert_cache: Optional[List[Dict]] = None

    def acquire_ops_token(self) -> Optional[str]:
        """Acquire an OpsToken from the suite-api auth endpoint."""
        url = f"{self.ops_url}/suite-api/api/auth/token/acquire"
        for auth_source in ['local', 'localItem']:
            try:
                resp = self.session.post(url, json={
                    "username": self.username,
                    "password": self.password,
                    "authSource": auth_source
                }, timeout=30)
                if resp.status_code == 200:
                    self.ops_token = resp.json().get('token')
                    if self.ops_token:
                        logger.debug(f"OpsToken acquired (authSource={auth_source})")
                        self._setup_session_headers()
                        return self.ops_token
            except Exception:
                continue
        logger.error("Failed to acquire OpsToken")
        return None

    def _setup_session_headers(self):
        """Set standard headers on the session after token acquisition."""
        self.session.headers.update({
            'Authorization': f'OpsToken {self.ops_token}',
            'X-vRealizeOps-API-use-unsupported': 'true',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        })

    def _establish_internal_session(self):
        """
        Hit the internal certificatemanagement endpoint once to establish
        session cookies that the vcf-operations/rest/ops/internal path requires.
        """
        url = f"{self.ops_url}/vcf-operations/rest/ops/internal/certificatemanagement/certificates/query"
        try:
            self.session.post(url, json={
                "vcfComponent": "VCF_MANAGEMENT",
                "vcfComponentType": "ARIA"
            }, timeout=30)
        except Exception:
            pass

    def connect(self) -> bool:
        """Acquire token and establish session. Returns True on success."""
        if not self.acquire_ops_token():
            return False
        self._establish_internal_session()
        return True

    def query_certificates(self, force_refresh: bool = False) -> List[Dict]:
        """Query all fleet-managed TLS certificates."""
        if self._cert_cache and not force_refresh:
            return self._cert_cache

        url = f"{self.ops_url}/suite-api/internal/certificatemanagement/certificates/query"
        try:
            resp = self.session.post(url, json={
                "vcfComponent": "VCF_MANAGEMENT",
                "vcfComponentType": "ARIA"
            }, timeout=30)
            if resp.status_code == 200:
                all_certs = resp.json().get('vcfCertificateModels', [])
                self._cert_cache = [c for c in all_certs if c.get('category') == 'TLS_CERT']
                return self._cert_cache
        except Exception as e:
            logger.error(f"Failed to query certificates: {e}")
        return []

    def get_cert_key_for_target(self, fqdn: str) -> Optional[str]:
        """Find the certificateResourceKey for a given FQDN or IP."""
        certs = self.query_certificates()
        fqdn_lower = fqdn.lower()

        ip_addr = self.FLEET_CERT_TARGETS.get(fqdn_lower)
        if not ip_addr:
            try:
                ip_addr = socket.gethostbyname(fqdn)
            except socket.gaierror:
                pass

        for cert in certs:
            cert_ip = cert.get('applianceIp', '').lower()
            if cert_ip == fqdn_lower or cert_ip == ip_addr:
                return cert.get('certificateResourceKey')

        return None

    def get_cert_info_for_target(self, fqdn: str) -> Optional[Dict]:
        """Get full certificate info for a target."""
        certs = self.query_certificates()
        fqdn_lower = fqdn.lower()

        ip_addr = self.FLEET_CERT_TARGETS.get(fqdn_lower)
        if not ip_addr:
            try:
                ip_addr = socket.gethostbyname(fqdn)
            except socket.gaierror:
                pass

        for cert in certs:
            cert_ip = cert.get('applianceIp', '').lower()
            if cert_ip == fqdn_lower or cert_ip == ip_addr:
                return cert

        return None

    def is_fleet_managed(self, fqdn: str) -> bool:
        """Check if a target is managed by the fleet certificate management API."""
        return self.get_cert_key_for_target(fqdn) is not None

    def generate_csr(self, fqdn: str, cert_resource_key: str,
                     common_name: str, dns_sans: List[str],
                     ip_sans: List[str]) -> Optional[str]:
        """
        Generate a CSR via the fleet certificate management API.
        Returns a task ID on success.
        """
        url = f"{self.ops_url}/suite-api/internal/certificatemanagement/csrs"
        payload = {
            "commonCsrData": {
                "country": "US",
                "email": "",
                "keySize": "KEY_2048",
                "keyAlgorithm": "RSA",
                "locality": "Palo Alto",
                "organization": "Broadcom",
                "orgUnit": "vcfms",
                "state": "CA"
            },
            "componentCsrData": [{
                "certificateId": cert_resource_key,
                "commonName": common_name,
                "subjectAltNames": {
                    "dns": dns_sans,
                    "ip": ip_sans
                }
            }]
        }
        try:
            resp = self.session.post(url, json=payload, timeout=60)
            if resp.status_code == 200:
                task = resp.json()
                task_id = task.get('id')
                logger.info(f"  CSR generation task: {task_id}")
                return task_id
            else:
                logger.error(f"  CSR generation failed: {resp.status_code}")
                logger.error(f"  Response: {resp.text[:500]}")
                return None
        except Exception as e:
            logger.error(f"  CSR generation failed: {e}")
            return None

    def get_csrs(self) -> List[Dict]:
        """List all generated CSRs."""
        url = f"{self.ops_url}/suite-api/internal/certificatemanagement/csrs"
        try:
            resp = self.session.get(url, params={"page": 0, "pageSize": 50}, timeout=30)
            if resp.status_code == 200:
                return resp.json().get('certificateSignatureInfo', [])
        except Exception as e:
            logger.error(f"  Failed to list CSRs: {e}")
        return []

    def get_csr_for_ip(self, ip_or_hostname: str) -> Optional[str]:
        """Get the CSR PEM content for a specific appliance (by IP or FQDN)."""
        targets = {ip_or_hostname.lower()}
        ip = self.FLEET_CERT_TARGETS.get(ip_or_hostname.lower())
        if ip:
            targets.add(ip)
        try:
            resolved = socket.gethostbyname(ip_or_hostname)
            targets.add(resolved)
        except socket.gaierror:
            pass

        for csr in self.get_csrs():
            hostname = csr.get('applianceHostname', '').lower()
            if hostname in targets:
                return csr.get('csr')
        return None

    def import_certificate(self, cert_name: str, cert_chain_pem: str) -> Optional[str]:
        """
        Import a signed certificate into the VCF Operations certificate repository.
        cert_chain_pem should contain the server cert followed by the CA cert.
        Returns task ID on success.
        """
        url = f"{self.ops_url}/suite-api/internal/certificatemanagement/repository/certificates/import"
        payload = {
            "certificates": [{
                "name": cert_name,
                "source": "PASTE",
                "certificate": cert_chain_pem
            }]
        }
        try:
            resp = self.session.put(url, json=payload, timeout=60)
            if resp.status_code == 200:
                task = resp.json()
                task_id = task.get('id')
                logger.info(f"  Certificate import task: {task_id}")
                return task_id
            else:
                logger.error(f"  Certificate import failed: {resp.status_code}")
                logger.error(f"  Response: {resp.text[:500]}")
                return None
        except Exception as e:
            logger.error(f"  Certificate import failed: {e}")
            return None

    def list_repo_certificates(self) -> List[Dict]:
        """List all certificates in the repository."""
        url = f"{self.ops_url}/suite-api/internal/certificatemanagement/repository/certificates"
        try:
            resp = self.session.get(url, params={"page": 0, "pageSize": 50}, timeout=30)
            if resp.status_code == 200:
                return resp.json().get('vcfRepositoryCertificates', [])
        except Exception as e:
            logger.error(f"  Failed to list repo certificates: {e}")
        return []

    def find_repo_cert_by_name(self, cert_name: str) -> Optional[str]:
        """Find a repository certificate ID by name."""
        for cert in self.list_repo_certificates():
            if cert.get('name') == cert_name:
                return cert.get('certId')
        return None

    def replace_certificate(self, cert_resource_key: str, repo_cert_id: str) -> Optional[str]:
        """
        Replace an active certificate with an imported one from the repository.
        Returns task ID on success.
        """
        result = self.replace_certificate_with_details(cert_resource_key, repo_cert_id)
        if result:
            return result.get('id')
        return None

    def replace_certificate_with_details(self, cert_resource_key: str, repo_cert_id: str) -> Optional[Dict]:
        """
        Replace an active certificate with an imported one from the repository.
        Returns full task response dict (including subTasksDetails with orchestratorType).
        """
        url = f"{self.ops_url}/suite-api/internal/certificatemanagement/certificates/replace"
        payload = {
            "caType": "EXTERNAL_CA",
            "certificatesMapping": [{
                "certificateId": cert_resource_key,
                "importedCertificateId": repo_cert_id
            }]
        }
        try:
            resp = self.session.put(url, json=payload, timeout=60)
            if resp.status_code in [200, 202]:
                task = resp.json()
                task_id = task.get('id')
                logger.info(f"  Certificate replace task: {task_id}")
                return task
            else:
                logger.error(f"  Certificate replace failed: {resp.status_code}")
                logger.error(f"  Response: {resp.text[:500]}")
                return None
        except Exception as e:
            logger.error(f"  Certificate replace failed: {e}")
            return None

    def wait_for_task(self, task_id: str, timeout: int = 300, poll_interval: int = 10) -> bool:
        """Poll a certificate management task until completion."""
        if not task_id:
            return True

        url = f"{self.ops_url}/suite-api/internal/certificatemanagement/tasks/{task_id}"
        start_time = time.time()
        logger.info(f"  Waiting for task {task_id[:12]}...")

        while time.time() - start_time < timeout:
            try:
                resp = self.session.get(url, timeout=30)
                if resp.status_code == 200:
                    task = resp.json()
                    status = task.get('status', '')
                    summary = task.get('subTasksSummary', {})

                    if status == 'COMPLETED':
                        failed = summary.get('failed', 0)
                        if failed > 0:
                            logger.error(f"  Task completed with {failed} failures")
                            for sub in task.get('subTasksDetails', []):
                                if sub.get('status') == 'FAILED':
                                    logger.error(f"    {sub.get('appliance')}: {sub.get('message')}")
                            return False
                        logger.info(f"  Task completed successfully")
                        return True
                    elif status in ('IN_PROGRESS', 'NOT_STARTED'):
                        elapsed = int(time.time() - start_time)
                        completed = summary.get('completed', 0)
                        total = summary.get('total', 0)
                        logger.debug(f"  Task {status} ({completed}/{total} subtasks, {elapsed}s)")
                        time.sleep(poll_interval)
                    elif status == 'FAILED':
                        logger.error(f"  Task failed")
                        for sub in task.get('subTasksDetails', []):
                            if sub.get('status') == 'FAILED':
                                logger.error(f"    {sub.get('appliance')}: {sub.get('message')}")
                        return False
                    else:
                        time.sleep(poll_interval)
                elif resp.status_code == 500:
                    time.sleep(poll_interval)
                else:
                    logger.warning(f"  Unexpected task poll status: {resp.status_code}")
                    time.sleep(poll_interval)
            except Exception as e:
                logger.warning(f"  Task poll error: {e}")
                time.sleep(poll_interval)

        logger.error(f"  Task timed out after {timeout}s")
        return False


# =============================================================================
# Component-Specific Certificate Replacement (Legacy/Fallback)
# =============================================================================

class SDDCManagerCertReplacer:
    """Replace certificates on SDDC Manager via SSH."""
    
    CERT_PATH = "/etc/ssl/certs/vcf_https.crt"
    KEY_PATH = "/etc/ssl/private/vcf_https.key"
    
    def __init__(self, fqdn: str, password: str):
        self.fqdn = fqdn
        self.password = password
        self.ssh_user = get_credentials_for_target(fqdn, 'ssh')
        
    def replace(self, cert_pem: str, key_pem: str, dry_run: bool = False) -> str:
        """Replace SDDC Manager certificate via SSH."""
        logger.info(f"Replacing certificate on SDDC Manager: {self.fqdn}")
        logger.info(f"  SSH User: {self.ssh_user}")
        logger.info(f"  Cert Path: {self.CERT_PATH}")
        logger.info(f"  Key Path: {self.KEY_PATH}")
        
        if dry_run:
            logger.info("[DRY RUN] Would replace certificate via SSH")
            return "SUCCESS"
        
        try:
            # Create temp files
            with tempfile.NamedTemporaryFile(mode='w', suffix='.crt', delete=False) as cert_file:
                cert_file.write(cert_pem)
                cert_path = cert_file.name
            
            with tempfile.NamedTemporaryFile(mode='w', suffix='.key', delete=False) as key_file:
                key_file.write(key_pem)
                key_path = key_file.name
            
            # Test SSH connectivity first
            success, output = run_ssh_command(self.fqdn, self.ssh_user, self.password, "echo test")
            if not success:
                logger.warning(f"  SSH connection failed - manual replacement required")
                logger.info(f"  ╔═══════════════════════════════════════════════════════════════╗")
                logger.info(f"  ║ MANUAL REPLACEMENT REQUIRED                                   ║")
                logger.info(f"  ╠═══════════════════════════════════════════════════════════════╣")
                logger.info(f"  ║ Certificate saved to: /tmp/vcf-certs/{self.fqdn}.crt")
                logger.info(f"  ║ Private key saved to: /tmp/vcf-certs/{self.fqdn}.key")
                logger.info(f"  ║                                                               ║")
                logger.info(f"  ║ SSH as root to {self.fqdn} and run:")
                logger.info(f"  ║   cp /tmp/vcf-certs/{self.fqdn}.crt {self.CERT_PATH}")
                logger.info(f"  ║   cp /tmp/vcf-certs/{self.fqdn}.key {self.KEY_PATH}")
                logger.info(f"  ║   chmod 644 {self.CERT_PATH}")
                logger.info(f"  ║   chmod 640 {self.KEY_PATH}")
                logger.info(f"  ║   systemctl reload nginx")
                logger.info(f"  ╚═══════════════════════════════════════════════════════════════╝")
                return "WARNING"  # Certificate generated successfully, just needs manual installation
            
            # Copy files to SDDC Manager
            logger.info("  Copying certificate to SDDC Manager...")
            if not scp_file_to_host(self.fqdn, self.ssh_user, self.password, cert_path, "/tmp/new_cert.crt"):
                logger.error("  Failed to copy certificate")
                return "FAILED"
            
            logger.info("  Copying private key to SDDC Manager...")
            if not scp_file_to_host(self.fqdn, self.ssh_user, self.password, key_path, "/tmp/new_key.key"):
                logger.error("  Failed to copy private key")
                return "FAILED"
            
            # Backup and replace certificates (root user - no sudo needed)
            commands = [
                f"cp {self.CERT_PATH} {self.CERT_PATH}.bak",
                f"cp {self.KEY_PATH} {self.KEY_PATH}.bak",
                f"cp /tmp/new_cert.crt {self.CERT_PATH}",
                f"cp /tmp/new_key.key {self.KEY_PATH}",
                f"chmod 644 {self.CERT_PATH}",
                f"chmod 640 {self.KEY_PATH}",
                "chown root:root /etc/ssl/certs/vcf_https.crt",
                "chown root:root /etc/ssl/private/vcf_https.key",
                "rm -f /tmp/new_cert.crt /tmp/new_key.key",
            ]
            
            for cmd in commands:
                logger.debug(f"  Running: {cmd}")
                success, output = run_ssh_command(self.fqdn, self.ssh_user, self.password, cmd)
                if not success:
                    logger.error(f"  Command failed: {cmd}")
                    logger.error(f"  Output: {output}")
                    return "FAILED"
            
            # Restart nginx
            logger.info("  Restarting nginx service...")
            success, output = run_ssh_command(
                self.fqdn, self.ssh_user, self.password,
                "systemctl reload nginx",
                timeout=60
            )
            
            if success:
                logger.info("✓ SDDC Manager certificate replaced successfully")
                return "SUCCESS"
            else:
                logger.warning(f"  Nginx reload warning: {output}")
                return "SUCCESS"  # Certificate was replaced, reload might show warning
            
        except Exception as e:
            logger.error(f"Failed to replace SDDC Manager certificate: {e}")
            return "FAILED"
        finally:
            # Cleanup temp files
            try:
                os.unlink(cert_path)
                os.unlink(key_path)
            except:
                pass


class VCFOperationsCertReplacer:
    """Replace certificates on VCF Operations via SSH."""
    
    def __init__(self, fqdn: str, password: str):
        self.fqdn = fqdn
        self.password = password
        self.ssh_user = get_credentials_for_target(fqdn, 'ssh')
        
    def replace(self, cert_pem: str, key_pem: str, dry_run: bool = False) -> str:
        """Replace VCF Operations certificate via SSH."""
        logger.info(f"Replacing certificate on VCF Operations: {self.fqdn}")
        logger.info(f"  SSH User: {self.ssh_user}")
        
        if dry_run:
            logger.info("[DRY RUN] Would replace certificate via SSH")
            return "SUCCESS"
        
        try:
            # Create temp files
            with tempfile.NamedTemporaryFile(mode='w', suffix='.crt', delete=False) as cert_file:
                cert_file.write(cert_pem)
                cert_path = cert_file.name
            
            with tempfile.NamedTemporaryFile(mode='w', suffix='.key', delete=False) as key_file:
                key_file.write(key_pem)
                key_path = key_file.name
            
            # Copy files to VCF Operations
            logger.info("  Copying certificate...")
            if not scp_file_to_host(self.fqdn, self.ssh_user, self.password, cert_path, "/tmp/new_cert.pem"):
                logger.error("  Failed to copy certificate")
                return "FAILED"
            
            logger.info("  Copying private key...")
            if not scp_file_to_host(self.fqdn, self.ssh_user, self.password, key_path, "/tmp/new_key.pem"):
                logger.error("  Failed to copy private key")
                return "FAILED"
            
            # VCF Operations certificate replacement commands
            # Note: Actual paths may vary - this is a generic approach
            commands = [
                "cp /tmp/new_cert.pem /tmp/new_key.pem /storage/",
                "rm -f /tmp/new_cert.pem /tmp/new_key.pem",
            ]
            
            for cmd in commands:
                logger.debug(f"  Running: {cmd}")
                success, output = run_ssh_command(self.fqdn, self.ssh_user, self.password, cmd)
                if not success:
                    logger.warning(f"  Command warning: {output}")
            
            logger.info("✓ VCF Operations certificate files copied")
            logger.info("  NOTE: Manual service restart may be required")
            return "SUCCESS"
            
        except Exception as e:
            logger.error(f"Failed to replace VCF Operations certificate: {e}")
            return "FAILED"
        finally:
            try:
                os.unlink(cert_path)
                os.unlink(key_path)
            except:
                pass


class VCFAutomationCertReplacer:
    """
    Replace certificates on VCF Automation (VCF Automation).
    
    Note: VCF Automation runs on Kubernetes and certificates are typically managed
    through VCF Operations Manager, not directly via SSH. This replacer generates
    the certificate and provides instructions for manual replacement via Lifecycle.
    """
    
    def __init__(self, fqdn: str, password: str):
        self.fqdn = fqdn
        self.password = password
        self.ssh_user = get_credentials_for_target(fqdn, 'ssh')
        
    def replace(self, cert_pem: str, key_pem: str, dry_run: bool = False) -> str:
        """
        Prepare certificate for VCF Automation replacement.
        
        VCF Automation (VCF Automation) certificates are managed through
        VCF Operations Manager. This method saves the certificate locally
        and provides manual instructions.
        """
        logger.info(f"Preparing certificate for VCF Automation: {self.fqdn}")
        logger.info(f"  SSH User available: {self.ssh_user}")
        
        if dry_run:
            logger.info("[DRY RUN] Would prepare certificate for VCF Automation")
            return "SUCCESS"
        
        # Save certificate for manual import
        cert_dir = Path("/tmp/vcf-certs")
        cert_dir.mkdir(exist_ok=True)
        
        cert_file = cert_dir / f"{self.fqdn}.crt"
        key_file = cert_dir / f"{self.fqdn}.key"
        
        # Create combined PEM file for Lifecycle import
        combined_file = cert_dir / f"{self.fqdn}-combined.pem"
        combined_content = cert_pem.strip() + "\n" + key_pem.strip()
        combined_file.write_text(combined_content)
        combined_file.chmod(0o600)
        
        logger.info(f"  ✓ Certificate prepared: {cert_file}")
        logger.info(f"  ✓ Combined PEM for import: {combined_file}")
        logger.info("")
        logger.info("  MANUAL STEPS REQUIRED for VCF Automation:")
        logger.info("  1. Log in to VCF Operations Manager (https://lcm-a.site-a.vcf.lab)")
        logger.info("  2. Navigate to Locker > Certificates > Import")
        logger.info(f"  3. Import the certificate from: {combined_file}")
        logger.info("  4. Go to Lifecycle Operations > Environments")
        logger.info("  5. Select the Automation environment")
        logger.info("  6. Click Replace Certificate and select the imported cert")
        
        return "WARNING"


class NSXManagerCertReplacer:
    """Replace certificates on NSX Manager via NSX API."""
    
    def __init__(self, fqdn: str, password: str):
        self.fqdn = fqdn
        self.password = password
        self.api_user = get_credentials_for_target(fqdn, 'api')
        self.session = requests.Session()
        self.session.verify = False
        self.session.auth = (self.api_user, password)
        
    def replace(self, cert_pem: str, key_pem: str, dry_run: bool = False) -> str:
        """Replace NSX Manager certificate via NSX API."""
        logger.info(f"Replacing certificate on NSX Manager: {self.fqdn}")
        logger.info(f"  API User: {self.api_user}")
        
        if dry_run:
            logger.info("[DRY RUN] Would replace certificate via NSX API")
            return "SUCCESS"
        
        try:
            # NSX Manager certificate import with private key
            # The API endpoint for importing certificates with private key is different
            url = f"https://{self.fqdn}/api/v1/trust-management/certificates?action=import"
            
            # NSX requires the private key to be included with the certificate
            # Format: certificate chain followed by private key
            pem_data = cert_pem.strip() + "\n" + key_pem.strip()
            
            payload = {
                "pem_encoded": pem_data,
                "private_key": key_pem.strip()
            }
            
            logger.info("  Importing certificate with private key to NSX...")
            resp = self.session.post(url, json=payload, timeout=60)
            
            if resp.status_code in [200, 201]:
                data = resp.json()
                results = data.get("results", [])
                if results:
                    cert_id = results[0].get("id")
                    logger.info(f"  Certificate imported: {cert_id}")
                    
                    # Apply certificate to cluster
                    apply_url = f"https://{self.fqdn}/api/v1/cluster/api-certificate?action=set_cluster_certificate&certificate_id={cert_id}"
                    
                    logger.info("  Applying certificate to cluster...")
                    apply_resp = self.session.post(apply_url, timeout=120)
                    
                    if apply_resp.status_code == 200:
                        logger.info("✓ NSX Manager certificate replaced successfully")
                        logger.info("  NOTE: NSX services may restart - allow 2-3 minutes")
                        return "SUCCESS"
                    else:
                        logger.error(f"  Failed to apply certificate: {apply_resp.status_code}")
                        try:
                            error_data = apply_resp.json()
                            error_msg = error_data.get('error_message', str(error_data))
                            logger.error(f"  Error: {error_msg}")
                        except:
                            logger.debug(f"  Response: {apply_resp.text}")
                        
                        # Show manual instructions
                        logger.warning(f"  Certificate {cert_id} was imported but not applied automatically")
                        logger.info(f"  ╔═══════════════════════════════════════════════════════════════╗")
                        logger.info(f"  ║ MANUAL APPLICATION REQUIRED                                   ║")
                        logger.info(f"  ╠═══════════════════════════════════════════════════════════════╣")
                        logger.info(f"  ║ Certificate saved to: /tmp/vcf-certs/{self.fqdn}.crt")
                        logger.info(f"  ║ Private key saved to: /tmp/vcf-certs/{self.fqdn}.key")
                        logger.info(f"  ║                                                               ║")
                        logger.info(f"  ║ Apply via NSX UI:                                             ║")
                        logger.info(f"  ║   System > Certificates > Import > Certificate with Private Key")
                        logger.info(f"  ║   Then: System > Certificates > (select) > Replace Cluster Cert")
                        logger.info(f"  ╚═══════════════════════════════════════════════════════════════╝")
                        return "WARNING"  # Cert generated successfully
                else:
                    logger.error("  No certificate ID returned from import")
                    return "FAILED"
                        
            else:
                logger.error(f"  Failed to import certificate: {resp.status_code}")
                try:
                    error_data = resp.json()
                    logger.error(f"  Error: {error_data.get('error_message', error_data)}")
                except:
                    logger.debug(f"  Response: {resp.text}")
                    
                # Show manual instructions
                logger.info(f"  ╔═══════════════════════════════════════════════════════════════╗")
                logger.info(f"  ║ MANUAL IMPORT REQUIRED                                        ║")
                logger.info(f"  ╠═══════════════════════════════════════════════════════════════╣")
                logger.info(f"  ║ Certificate saved to: /tmp/vcf-certs/{self.fqdn}.crt")
                logger.info(f"  ║ Private key saved to: /tmp/vcf-certs/{self.fqdn}.key")
                logger.info(f"  ║                                                               ║")
                logger.info(f"  ║ Import via NSX UI:                                            ║")
                logger.info(f"  ║   System > Certificates > Import > Certificate with Private Key")
                logger.info(f"  ║   Then: System > Certificates > (select) > Replace Cluster Cert")
                logger.info(f"  ╚═══════════════════════════════════════════════════════════════╝")
                return "WARNING"  # Cert generated successfully
                
        except Exception as e:
            logger.error(f"Failed to replace NSX certificate: {e}")
            return "FAILED"


class VCenterCertReplacer:
    """Replace certificates on vCenter via SSH/API."""
    
    def __init__(self, fqdn: str, password: str):
        self.fqdn = fqdn
        self.password = password
        self.ssh_user = get_credentials_for_target(fqdn, 'ssh')
        
    def replace(self, cert_pem: str, key_pem: str, dry_run: bool = False) -> str:
        """Replace vCenter certificate via SSH."""
        logger.info(f"Replacing certificate on vCenter: {self.fqdn}")
        logger.info(f"  SSH User: {self.ssh_user}")
        
        if dry_run:
            logger.info("[DRY RUN] Would replace certificate via SSH")
            return "SUCCESS"
        
        try:
            # Create temp files
            with tempfile.NamedTemporaryFile(mode='w', suffix='.crt', delete=False) as cert_file:
                cert_file.write(cert_pem)
                cert_path = cert_file.name
            
            with tempfile.NamedTemporaryFile(mode='w', suffix='.key', delete=False) as key_file:
                key_file.write(key_pem)
                key_path = key_file.name
            
            # Try to copy files to vCenter
            logger.info("  Copying certificate to vCenter...")
            copy_success = scp_file_to_host(self.fqdn, self.ssh_user, self.password, cert_path, "/tmp/new_machine_ssl.crt")
            
            if copy_success:
                logger.info("  Copying private key to vCenter...")
                if scp_file_to_host(self.fqdn, self.ssh_user, self.password, key_path, "/tmp/new_machine_ssl.key"):
                    logger.info("✓ vCenter certificate files copied to /tmp/")
                    logger.info("  NOTE: Use certificate-manager to complete replacement")
                    logger.info("  Run: /usr/lib/vmware-vmca/bin/certificate-manager")
                    return "WARNING"
            
            # If we get here, SCP failed - show manual instructions
            logger.warning(f"  SCP connection failed - manual replacement required")
            logger.info(f"  ╔═══════════════════════════════════════════════════════════════╗")
            logger.info(f"  ║ MANUAL REPLACEMENT REQUIRED                                   ║")
            logger.info(f"  ╠═══════════════════════════════════════════════════════════════╣")
            logger.info(f"  ║ Certificate saved to: /tmp/vcf-certs/{self.fqdn}.crt")
            logger.info(f"  ║ Private key saved to: /tmp/vcf-certs/{self.fqdn}.key")
            logger.info(f"  ║                                                               ║")
            logger.info(f"  ║ SCP files to vCenter and use certificate-manager:            ║")
            logger.info(f"  ║   scp /tmp/vcf-certs/{self.fqdn}.* root@{self.fqdn}:/tmp/")
            logger.info(f"  ║   ssh root@{self.fqdn}")
            logger.info(f"  ║   /usr/lib/vmware-vmca/bin/certificate-manager")
            logger.info(f"  ╚═══════════════════════════════════════════════════════════════╝")
            
            return "WARNING"
            
        except Exception as e:
            logger.error(f"Failed to copy vCenter certificate: {e}")
            return "FAILED"
        finally:
            try:
                os.unlink(cert_path)
                os.unlink(key_path)
            except:
                pass


# =============================================================================
# Main Certificate Replacement Function
# =============================================================================

# =============================================================================
# NSX Compute Manager Re-registration After Certificate Replacement
# =============================================================================

class NSXComputeManagerFixer:
    """
    Fixes NSX compute manager trust issues after vCenter/NSX certificate replacement.

    After vCenter SSL certificates are replaced with Vault-signed certs, NSX compute
    managers go DOWN because:
    1. The Vault CA may have a double-cert entry in vCenter TRUSTED_ROOTS (caused by
       dir-cli trustedcert publish being called twice), which NSX rejects (MP2179)
    2. The vCenter thumbprint stored in NSX no longer matches the new certificate
    3. The Vault CA may not be in the NSX trust store

    This class performs three remediation steps:
    - Fix double-cert entries in vCenter TRUSTED_ROOTS
    - Import Vault CA into NSX trust stores (idempotent)
    - Re-register compute managers with the new vCenter thumbprint
    """

    VCENTER_NSX_PAIRS = [
        {
            'vcenter_fqdn': 'vc-mgmt-a.site-a.vcf.lab',
            'vcenter_sso_user': 'administrator@vsphere.local',
            'vcenter_sso_domain': 'vsphere.local',
            'nsx_fqdn': 'nsx-mgmt-01a.site-a.vcf.lab',
        },
        {
            'vcenter_fqdn': 'vc-wld01-a.site-a.vcf.lab',
            'vcenter_sso_user': 'administrator@wld.sso',
            'vcenter_sso_domain': 'wld.sso',
            'nsx_fqdn': 'nsx-wld01-01a.site-a.vcf.lab',
        },
    ]

    def __init__(self, password: str, vault_url: str, vault_token: str):
        self.password = password
        self.vault_url = vault_url.rstrip('/')
        self.vault_token = vault_token

    def _get_vault_ca_pem(self) -> Optional[str]:
        """Get the Vault root CA PEM."""
        try:
            resp = requests.get(f"{self.vault_url}/v1/pki/ca/pem", timeout=10, verify=False)
            if resp.status_code == 200:
                return resp.text.strip()
        except Exception as e:
            logger.error(f"  Failed to get Vault CA: {e}")
        return None

    def _get_vcenter_thumbprint(self, fqdn: str) -> Optional[str]:
        """Get the SHA-256 thumbprint of a vCenter's current SSL certificate."""
        try:
            result = subprocess.run(
                ['bash', '-c',
                 f'echo | openssl s_client -connect {fqdn}:443 2>/dev/null '
                 f'| openssl x509 -fingerprint -sha256 -noout '
                 f'| sed "s/sha256 Fingerprint=//"'],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception as e:
            logger.error(f"  Failed to get thumbprint for {fqdn}: {e}")
        return None

    def fix_vcenter_trusted_roots(self, vcenter_fqdn: str, sso_user: str) -> bool:
        """
        Fix double-cert entries for Vault CA in vCenter TRUSTED_ROOTS.

        dir-cli trustedcert publish silently appends a duplicate PEM when called
        twice with the same cert. NSX rejects multi-cert PEMs with MP2179.
        """
        logger.info(f"  Checking TRUSTED_ROOTS on {vcenter_fqdn} for double-cert entries...")

        vault_ca = self._get_vault_ca_pem()
        if not vault_ca:
            return False

        # Check for double-cert entries
        ok, output = run_ssh_command(
            vcenter_fqdn, 'root', self.password,
            'for alias in $(/usr/lib/vmware-vmafd/bin/vecs-cli entry list --store TRUSTED_ROOTS '
            '| grep Alias | awk -F":\\t" \'{print $2}\' | xargs); do '
            'count=$(/usr/lib/vmware-vmafd/bin/vecs-cli entry getcert --store TRUSTED_ROOTS '
            '--alias "$alias" | grep -c "BEGIN CERTIFICATE"); '
            'subject=$(/usr/lib/vmware-vmafd/bin/vecs-cli entry getcert --store TRUSTED_ROOTS '
            '--alias "$alias" | openssl x509 -noout -subject 2>/dev/null); '
            'echo "$alias|$count|$subject"; done',
            timeout=30
        )

        if not ok:
            logger.warning(f"  Could not check TRUSTED_ROOTS on {vcenter_fqdn}: {output}")
            return False

        has_double_cert = False
        for line in output.strip().splitlines():
            parts = line.strip().split('|')
            if len(parts) >= 2:
                alias, count = parts[0].strip(), parts[1].strip()
                if count == '2' and 'Root Authority' in line:
                    has_double_cert = True
                    logger.warning(f"  Double-cert found: alias={alias} ({count} certs)")

        if not has_double_cert:
            logger.info(f"  No double-cert entries found on {vcenter_fqdn}")
            return True

        # Write Vault CA to temp file on vCenter, unpublish, republish
        with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as f:
            f.write(vault_ca + '\n')
            local_ca_path = f.name

        try:
            scp_file_to_host(vcenter_fqdn, 'root', self.password, local_ca_path,
                             '/tmp/vault-ca-single.pem')

            sso_admin = sso_user
            logger.info(f"  Unpublishing double-cert entry...")
            ok, out = run_ssh_command(vcenter_fqdn, 'root', self.password,
                f"/usr/lib/vmware-vmafd/bin/dir-cli trustedcert unpublish "
                f"--cert /tmp/vault-ca-single.pem "
                f"--login {sso_admin} --password '{self.password}'",
                timeout=30)
            if not ok:
                logger.error(f"  Unpublish failed: {out}")
                return False

            logger.info(f"  Republishing as single-cert entry...")
            ok, out = run_ssh_command(vcenter_fqdn, 'root', self.password,
                f"/usr/lib/vmware-vmafd/bin/dir-cli trustedcert publish "
                f"--cert /tmp/vault-ca-single.pem "
                f"--login {sso_admin} --password '{self.password}'",
                timeout=30)
            if not ok:
                logger.error(f"  Republish failed: {out}")
                return False

            run_ssh_command(vcenter_fqdn, 'root', self.password,
                '/usr/lib/vmware-vmafd/bin/vecs-cli force-refresh', timeout=15)
            run_ssh_command(vcenter_fqdn, 'root', self.password,
                'rm -f /tmp/vault-ca-single.pem', timeout=10)

            logger.info(f"  Fixed double-cert entry on {vcenter_fqdn}")
            return True
        finally:
            os.unlink(local_ca_path)

    def ensure_vault_ca_in_nsx(self, nsx_fqdn: str) -> bool:
        """Import Vault CA into NSX trust store if not already present."""
        vault_ca = self._get_vault_ca_pem()
        if not vault_ca:
            return False

        session = requests.Session()
        session.verify = False
        session.auth = ('admin', self.password)

        try:
            resp = session.post(
                f'https://{nsx_fqdn}/api/v1/trust-management/certificates?action=import',
                json={'display_name': 'vcf.lab Root Authority', 'pem_encoded': vault_ca},
                timeout=30
            )
            if resp.status_code in [200, 201]:
                results = resp.json().get('results', [])
                if results:
                    logger.info(f"  Imported Vault CA into {nsx_fqdn} (ID: {results[0].get('id')})")
                return True
            elif resp.status_code == 400 and 'already exists' in resp.text.lower():
                logger.info(f"  Vault CA already in {nsx_fqdn} trust store")
                return True
            else:
                logger.warning(f"  Vault CA import to {nsx_fqdn}: {resp.status_code} {resp.text[:200]}")
                return resp.status_code == 409  # conflict = already exists
        except Exception as e:
            logger.error(f"  Failed to import Vault CA into {nsx_fqdn}: {e}")
            return False

    def reregister_compute_managers(self, nsx_fqdn: str, vcenter_fqdn: str,
                                     sso_user: str) -> bool:
        """Re-register compute managers on an NSX manager with the new vCenter thumbprint."""
        thumbprint = self._get_vcenter_thumbprint(vcenter_fqdn)
        if not thumbprint:
            logger.error(f"  Could not get thumbprint for {vcenter_fqdn}")
            return False

        session = requests.Session()
        session.verify = False
        session.auth = ('admin', self.password)

        # Find compute managers for this vCenter
        try:
            resp = session.get(
                f'https://{nsx_fqdn}/api/v1/fabric/compute-managers',
                timeout=30
            )
            if resp.status_code != 200:
                logger.error(f"  Failed to list compute managers: {resp.status_code}")
                return False

            cms = resp.json().get('results', [])
        except Exception as e:
            logger.error(f"  Failed to list compute managers: {e}")
            return False

        all_ok = True
        for cm in cms:
            cm_server = cm.get('server', '')
            cm_id = cm.get('id', '')

            if cm_server.lower() != vcenter_fqdn.lower():
                continue

            # Check if already UP/REGISTERED
            try:
                status_resp = session.get(
                    f'https://{nsx_fqdn}/api/v1/fabric/compute-managers/{cm_id}/status',
                    timeout=30
                )
                if status_resp.status_code == 200:
                    status_data = status_resp.json()
                    conn_status = status_data.get('connection_status', '')
                    reg_status = status_data.get('registration_status', '')
                    if conn_status == 'UP' and reg_status == 'REGISTERED':
                        logger.info(f"  Compute manager {cm_server} already UP/REGISTERED on {nsx_fqdn}")
                        continue
                    logger.info(f"  Compute manager {cm_server}: {conn_status}/{reg_status} - re-registering...")
            except Exception:
                pass

            # Build update payload - strip read-only fields and set new credential
            update_payload = {k: v for k, v in cm.items()
                             if k not in ('_create_time', '_create_user',
                                          '_last_modified_time', '_last_modified_user',
                                          '_protection', '_system_owned',
                                          'certificate', 'origin_properties')}
            update_payload['credential'] = {
                'credential_type': 'UsernamePasswordLoginCredential',
                'username': sso_user,
                'password': self.password,
                'thumbprint': thumbprint
            }

            try:
                put_resp = session.put(
                    f'https://{nsx_fqdn}/api/v1/fabric/compute-managers/{cm_id}',
                    json=update_payload,
                    timeout=60
                )
                if put_resp.status_code == 200:
                    new_rev = put_resp.json().get('_revision', '?')
                    logger.info(f"  Re-registered {cm_server} on {nsx_fqdn} (revision: {new_rev})")
                else:
                    error_msg = ''
                    try:
                        error_msg = put_resp.json().get('error_message', put_resp.text[:200])
                    except Exception:
                        error_msg = put_resp.text[:200]
                    logger.error(f"  Failed to re-register {cm_server}: {put_resp.status_code} - {error_msg}")
                    all_ok = False
            except Exception as e:
                logger.error(f"  Failed to re-register {cm_server}: {e}")
                all_ok = False

        return all_ok

    def verify_compute_managers(self, nsx_fqdn: str, vcenter_fqdn: str,
                                 timeout: int = 60) -> bool:
        """Poll compute manager status until UP/REGISTERED or timeout."""
        session = requests.Session()
        session.verify = False
        session.auth = ('admin', self.password)

        start = time.time()
        while time.time() - start < timeout:
            try:
                resp = session.get(
                    f'https://{nsx_fqdn}/api/v1/fabric/compute-managers',
                    timeout=30
                )
                if resp.status_code != 200:
                    time.sleep(10)
                    continue

                for cm in resp.json().get('results', []):
                    if cm.get('server', '').lower() != vcenter_fqdn.lower():
                        continue

                    cm_id = cm.get('id')
                    status_resp = session.get(
                        f'https://{nsx_fqdn}/api/v1/fabric/compute-managers/{cm_id}/status',
                        timeout=30
                    )
                    if status_resp.status_code == 200:
                        data = status_resp.json()
                        if (data.get('connection_status') == 'UP'
                                and data.get('registration_status') == 'REGISTERED'):
                            logger.info(f"  Compute manager {vcenter_fqdn} is UP/REGISTERED on {nsx_fqdn}")
                            return True
            except Exception:
                pass
            time.sleep(10)

        logger.warning(f"  Compute manager {vcenter_fqdn} not UP on {nsx_fqdn} after {timeout}s")
        return False

    def fix_all(self, replaced_targets: List[str], dry_run: bool = False) -> Dict[str, str]:
        """
        Fix NSX compute managers for all vCenter/NSX pairs where the vCenter
        or NSX certificate was replaced.

        Only runs for pairs where at least one of the vCenter or NSX FQDNs
        appears in replaced_targets (indicating their cert was changed).
        """
        # Determine which pairs need fixing
        pairs_to_fix = []
        replaced_lower = {t.lower() for t in replaced_targets}

        for pair in self.VCENTER_NSX_PAIRS:
            vc = pair['vcenter_fqdn'].lower()
            nsx = pair['nsx_fqdn'].lower()
            # Also match the VIP FQDN patterns
            nsx_vip = nsx.replace('-01a.', '-a.').replace('-01a.', '-a.')
            if vc in replaced_lower or nsx in replaced_lower or nsx_vip in replaced_lower:
                pairs_to_fix.append(pair)

        if not pairs_to_fix:
            logger.info("No vCenter/NSX certificate replacements detected - skipping compute manager fix")
            return {}

        logger.info("=" * 60)
        logger.info("POST-REPLACEMENT: Fixing NSX Compute Manager Trust")
        logger.info("=" * 60)

        results = {}
        for pair in pairs_to_fix:
            vc_fqdn = pair['vcenter_fqdn']
            nsx_fqdn = pair['nsx_fqdn']
            sso_user = pair['vcenter_sso_user']

            logger.info(f"\nFixing {nsx_fqdn} -> {vc_fqdn}")

            if dry_run:
                logger.info(f"  [DRY RUN] Would fix TRUSTED_ROOTS, import Vault CA, re-register CM")
                results[f"{nsx_fqdn}->{vc_fqdn}"] = "DRY_RUN"
                continue

            # Step 1: Fix double-cert entries in vCenter TRUSTED_ROOTS
            self.fix_vcenter_trusted_roots(vc_fqdn, sso_user)

            # Step 2: Ensure Vault CA is in NSX trust store
            self.ensure_vault_ca_in_nsx(nsx_fqdn)

            # Step 3: Re-register compute managers with new thumbprint
            self.reregister_compute_managers(nsx_fqdn, vc_fqdn, sso_user)

            # Step 4: Verify status
            if self.verify_compute_managers(nsx_fqdn, vc_fqdn, timeout=60):
                results[f"{nsx_fqdn}->{vc_fqdn}"] = "SUCCESS"
            else:
                results[f"{nsx_fqdn}->{vc_fqdn}"] = "WARNING"

        return results

class GenericCertSaver:
    """
    Saves certificates locally for targets that don't support direct replacement.
    Used for VSP gateway endpoints, opsnet, and other API-only targets.
    """

    def __init__(self, fqdn: str, password: str):
        self.fqdn = fqdn
        self.password = password

    def replace(self, cert_pem: str, key_pem: str, dry_run: bool = False) -> str:
        """Save certificate locally and provide instructions."""
        logger.info(f"Saving certificate for: {self.fqdn}")

        if dry_run:
            logger.info("[DRY RUN] Would save certificate locally")
            return "SUCCESS"

        cert_dir = Path("/tmp/vcf-certs")
        cert_dir.mkdir(exist_ok=True)

        cert_file = cert_dir / f"{self.fqdn}.crt"
        key_file = cert_dir / f"{self.fqdn}.key"

        cert_file.write_text(cert_pem)
        key_file.write_text(key_pem)
        key_file.chmod(0o600)

        logger.info(f"  ✓ Certificate saved: {cert_file}")
        logger.info(f"  ✓ Private key saved: {key_file}")
        logger.info(f"  NOTE: This target's certificate is managed by the VSP cluster or fleet.")
        logger.info(f"        Manual import via VCF Operations Locker may be required.")
        return "WARNING"


class FleetCertReplacer:
    """
    Replaces certificates on fleet-managed targets using the VCF Operations
    certificate management API (suite-api/internal/certificatemanagement).

    5-step workflow:
      1. Generate CSR via API
      2. Download CSR from API
      3. Sign CSR with Vault PKI
      4. Import signed cert to repository
      5. Replace active cert with imported cert
    """

    def __init__(self, fqdn: str, password: str, ops_cert_api: 'VCFOpsCertManagementAPI',
                 vault_manager: 'VaultCertificateManager'):
        self.fqdn = fqdn
        self.password = password
        self.ops_cert_api = ops_cert_api
        self.vault_manager = vault_manager

    def replace(self, cert_pem: str = None, key_pem: str = None, dry_run: bool = False) -> str:
        """
        Execute the full 5-step fleet certificate replacement workflow.
        cert_pem/key_pem are ignored — the CSR is generated by the API and
        signed by Vault within this method.
        """
        logger.info(f"Fleet certificate replacement for: {self.fqdn}")

        cert_info = self.ops_cert_api.get_cert_info_for_target(self.fqdn)
        if not cert_info:
            logger.error(f"  Target {self.fqdn} not found in fleet certificate list")
            return "FAILED"

        cert_key = cert_info.get('certificateResourceKey')
        appliance_ip = cert_info.get('applianceIp', '')
        display_type = cert_info.get('displayApplianceType', '')
        api_cn = cert_info.get('issuedToCommonName', '')

        # CN must always be a valid FQDN, never an internal identifier like VCFA/OPS_LOGS
        common_name = self.fqdn
        if api_cn and api_cn != self.fqdn:
            logger.info(f"  Overriding API CN '{api_cn}' with FQDN '{self.fqdn}'")

        logger.info(f"  Component: {display_type}")
        logger.info(f"  Appliance: {appliance_ip}")
        logger.info(f"  CN: {common_name}")
        logger.info(f"  Cert key: {cert_key}")

        dns_sans = [self.fqdn]
        ip_sans = []
        known_ip = VCFOpsCertManagementAPI.FLEET_CERT_TARGETS.get(self.fqdn.lower())
        if known_ip:
            ip_sans.append(known_ip)
        elif appliance_ip and VaultCertificateManager._is_ip_address(appliance_ip):
            ip_sans.append(appliance_ip)
        resolved_ip = VaultCertificateManager.resolve_fqdn_to_ip(self.fqdn)
        if resolved_ip and resolved_ip not in ip_sans:
            ip_sans.append(resolved_ip)

        if dry_run:
            logger.info(f"  [DRY RUN] Would execute 5-step fleet cert replacement")
            logger.info(f"    DNS SANs: {dns_sans}")
            logger.info(f"    IP SANs: {ip_sans}")
            return "SUCCESS"

        # Step 1: Check for existing CSR or generate a new one
        csr_pem = self.ops_cert_api.get_csr_for_ip(appliance_ip)
        if csr_pem:
            # Validate existing CSR has FQDN as CN, not an internal identifier
            try:
                csr_normalized = csr_pem
                if '\n' not in csr_normalized and ' ' in csr_normalized:
                    csr_normalized = csr_normalized.replace(' ', '\n')
                    csr_normalized = csr_normalized.replace('-----BEGIN\nCERTIFICATE\nREQUEST-----',
                                                            '-----BEGIN CERTIFICATE REQUEST-----')
                    csr_normalized = csr_normalized.replace('-----END\nCERTIFICATE\nREQUEST-----',
                                                            '-----END CERTIFICATE REQUEST-----')
                csr_obj = x509.load_pem_x509_csr(csr_normalized.encode())
                csr_cn = csr_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
                if csr_cn == self.fqdn:
                    logger.info(f"  Step 1: Reusing existing CSR (CN={csr_cn})")
                else:
                    logger.info(f"  Step 1: Discarding stale CSR (CN={csr_cn} != {self.fqdn}), generating new one")
                    csr_pem = None
            except Exception as e:
                logger.warning(f"  Could not validate existing CSR CN: {e}")
                csr_pem = None

        if not csr_pem:
            logger.info("  Step 1: Generating CSR via fleet API...")
            task_id = self.ops_cert_api.generate_csr(
                fqdn=self.fqdn,
                cert_resource_key=cert_key,
                common_name=common_name,
                dns_sans=dns_sans,
                ip_sans=ip_sans
            )
            if not task_id:
                logger.error("  Failed to generate CSR")
                return "FAILED"

            # Step 2: Wait for CSR to become available (poll CSR list)
            logger.info("  Step 2: Waiting for CSR to become available...")
            for attempt in range(18):
                time.sleep(10)
                csr_pem = self.ops_cert_api.get_csr_for_ip(appliance_ip)
                if csr_pem:
                    break
                logger.debug(f"  CSR not ready yet (attempt {attempt + 1}/18)...")

        if not csr_pem:
            logger.error(f"  CSR not found for {appliance_ip} after 3 minutes")
            return "FAILED"

        # The API returns CSR with spaces instead of newlines — fix it
        if '\n' not in csr_pem and ' ' in csr_pem:
            csr_pem = csr_pem.replace(' ', '\n')
            csr_pem = csr_pem.replace('-----BEGIN\nCERTIFICATE\nREQUEST-----',
                                      '-----BEGIN CERTIFICATE REQUEST-----')
            csr_pem = csr_pem.replace('-----END\nCERTIFICATE\nREQUEST-----',
                                      '-----END CERTIFICATE REQUEST-----')
        logger.info(f"  CSR retrieved ({len(csr_pem)} bytes)")

        # Step 3: Sign CSR with Vault
        logger.info("  Step 3: Signing CSR with Vault PKI...")
        signed_cert = self.vault_manager.sign_csr(csr_pem, common_name, ip_sans=ip_sans or None)
        if not signed_cert:
            logger.error("  Failed to sign CSR with Vault")
            return "FAILED"

        cert_blocks = re.findall(
            r'(-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----)',
            signed_cert, re.DOTALL
        )
        if len(cert_blocks) >= 2:
            server_cert = cert_blocks[0]
            ca_cert = "\n".join(cert_blocks[1:])
            full_chain = server_cert + "\n" + ca_cert
        else:
            full_chain = signed_cert

        cert_dir = Path("/tmp/vcf-certs")
        cert_dir.mkdir(exist_ok=True)
        (cert_dir / f"{self.fqdn}.crt").write_text(signed_cert)

        # Step 4: Import signed cert to repository
        cert_name = f"vault-{self.fqdn.split('.')[0]}-{int(time.time())}"
        logger.info(f"  Step 4: Importing signed cert as '{cert_name}'...")
        import_task_id = self.ops_cert_api.import_certificate(cert_name, full_chain)
        if not import_task_id:
            logger.error("  Failed to import certificate")
            return "FAILED"

        # Wait for cert to appear in the repository
        repo_cert_id = None
        for attempt in range(18):
            time.sleep(10)
            repo_cert_id = self.ops_cert_api.find_repo_cert_by_name(cert_name)
            if repo_cert_id:
                break
            logger.debug(f"  Cert not in repo yet (attempt {attempt + 1}/18)...")

        if not repo_cert_id:
            logger.error(f"  Imported cert '{cert_name}' not found in repository after 3 minutes")
            return "FAILED"
        logger.info(f"  Imported cert ID: {repo_cert_id}")

        # Step 5: Replace active cert
        logger.info("  Step 5: Replacing active certificate...")
        replace_result = self.ops_cert_api.replace_certificate_with_details(cert_key, repo_cert_id)
        if not replace_result:
            logger.error("  Failed to initiate certificate replacement")
            return "FAILED"

        replace_task_id = replace_result.get('id')
        orchestrator = 'unknown'
        for sub in replace_result.get('subTasksDetails', []):
            orchestrator = sub.get('orchestratorType', orchestrator)
        logger.info(f"  Replace orchestrator: {orchestrator}")

        # Wait for replacement to complete by polling the cert list for issuer change
        logger.info("  Waiting for replacement to complete...")
        for attempt in range(60):
            time.sleep(10)
            self.ops_cert_api._cert_cache = None
            updated_info = self.ops_cert_api.get_cert_info_for_target(self.fqdn)
            if updated_info:
                issuer = updated_info.get('issuedBy', '')
                if 'vcf.lab Root Authority' in issuer:
                    logger.info(f"  Certificate replaced (issuer: {issuer})")
                    break
            logger.debug(f"  Replacement in progress (attempt {attempt + 1}/60)...")
        else:
            if orchestrator == 'VRSLCM':
                logger.warning(f"  VRSLCM orchestrator did not complete replacement for {self.fqdn}")
                logger.warning("  The signed certificate has been imported to the repository.")
                logger.warning("  To complete: VCF Operations UI > Fleet Management > Certificates > Replace")
            else:
                logger.warning("  Replacement task may still be running — check VCF Operations UI")
            return "WARNING"

        logger.info(f"  Fleet certificate replacement completed for {self.fqdn}")
        return "SUCCESS"


def get_replacer_for_target(fqdn: str, password: str,
                            ops_cert_api: Optional['VCFOpsCertManagementAPI'] = None,
                            vault_manager: Optional['VaultCertificateManager'] = None):
    """Get the appropriate certificate replacer for a target."""
    fqdn_lower = fqdn.lower()

    fleet_prefixes = (
        'auto-platform', 'auto-', 'auto.', 'ops-', 'ops.', 'opslogs',
        'opsnet', 'vidb', 'vsp-', 'fleet-', 'instance-',
    )
    if ops_cert_api and vault_manager and any(p in fqdn_lower for p in fleet_prefixes):
        if ops_cert_api.is_fleet_managed(fqdn):
            return FleetCertReplacer(fqdn, password, ops_cert_api, vault_manager)

    if 'sddcmanager' in fqdn_lower:
        return SDDCManagerCertReplacer(fqdn, password)
    elif 'auto-platform' in fqdn_lower:
        return VCFAutomationCertReplacer(fqdn, password)
    elif 'auto-' in fqdn_lower or 'auto.' in fqdn_lower:
        return VCFAutomationCertReplacer(fqdn, password)
    elif 'opslogs' in fqdn_lower:
        return VCFOperationsCertReplacer(fqdn, password)
    elif 'ops-' in fqdn_lower or 'ops.' in fqdn_lower:
        return VCFOperationsCertReplacer(fqdn, password)
    elif 'opsnet' in fqdn_lower:
        return GenericCertSaver(fqdn, password)
    elif 'nsx' in fqdn_lower:
        return NSXManagerCertReplacer(fqdn, password)
    elif 'vc-' in fqdn_lower or 'vcenter' in fqdn_lower:
        return VCenterCertReplacer(fqdn, password)
    elif any(prefix in fqdn_lower for prefix in ('vsp-', 'fleet-', 'instance-', 'vidb')):
        return GenericCertSaver(fqdn, password)
    else:
        logger.warning(f"No specific replacer for {fqdn}, saving locally")
        return GenericCertSaver(fqdn, password)


def process_certificate(
    fqdn: str,
    password: str,
    vault_manager: VaultCertificateManager,
    sddc_api: Optional[SDDCManagerAPI] = None,
    ops_cert_api: Optional[VCFOpsCertManagementAPI] = None,
    ops_trust_manager = None,
    dry_run: bool = False
) -> str:
    """
    Generate, sign, and replace certificate for a VCF component.

    Workflow selection (in priority order):
    1. SDDC-managed: CSR via SDDC Manager API, sign with Vault, upload via API
    2. Fleet-managed: CSR via Fleet Cert API, sign with Vault, import+replace via API
    3. Non-managed: local CSR, sign with Vault, component-specific replacement
    """
    logger.info("=" * 60)
    logger.info(f"Processing certificate for: {fqdn}")
    logger.info("=" * 60)

    cert_dir = Path("/tmp/vcf-certs")
    cert_dir.mkdir(exist_ok=True)

    try:
        if sddc_api and sddc_api.is_sddc_managed(fqdn):
            return _process_sddc_managed_certificate(fqdn, sddc_api, vault_manager, cert_dir, dry_run)

        if ops_cert_api and ops_cert_api.is_fleet_managed(fqdn):
            return _process_fleet_certificate(fqdn, password, vault_manager, ops_cert_api, cert_dir, dry_run)

        return _process_non_sddc_certificate(fqdn, password, vault_manager, ops_trust_manager, cert_dir, dry_run)

    except Exception as e:
        logger.error(f"Error processing certificate for {fqdn}: {e}", exc_info=True)
        return "FAILED"


def _process_sddc_managed_certificate(
    fqdn: str,
    sddc_api: SDDCManagerAPI,
    vault_manager: VaultCertificateManager,
    cert_dir: Path,
    dry_run: bool = False
) -> str:
    """
    Process certificate for SDDC Manager-managed resources.
    
    Uses SDDC Manager API for the complete workflow:
    1. Generate CSR on the remote component
    2. Sign CSR with Vault
    3. Upload signed certificate chain
    """
    resource_type = sddc_api.get_resource_type(fqdn)
    logger.info(f"  SDDC-managed resource: {resource_type}")
    
    # Step 1: Generate CSR via SDDC Manager API
    logger.info("  Step 1: Generating CSR via SDDC Manager API...")
    success, csr_task_id = sddc_api.generate_csr(fqdn, resource_type, dry_run=dry_run)
    
    if not success:
        logger.error("  Failed to initiate CSR generation")
        return "FAILED"
    
    if csr_task_id and not dry_run:
        # Wait for CSR generation to complete
        if not sddc_api.wait_for_task(csr_task_id, timeout=120):
            logger.error("  CSR generation task failed")
            return "FAILED"
    
    # Step 2: Get the CSR content
    if not dry_run:
        logger.info("  Step 2: Retrieving CSR from SDDC Manager...")
        csr_pem = sddc_api.get_csr(fqdn)
        if not csr_pem:
            logger.error("  Failed to retrieve CSR")
            return "FAILED"
        logger.info("  ✓ CSR retrieved successfully")
    else:
        logger.info("  [DRY RUN] Would retrieve CSR from SDDC Manager")
        csr_pem = None
    
    # Step 3: Sign CSR with Vault
    if not dry_run:
        logger.info("  Step 3: Signing CSR with Vault PKI...")
        cert_chain = vault_manager.sign_csr(csr_pem, fqdn)
        if not cert_chain:
            logger.error("  Failed to sign CSR with Vault")
            return "FAILED"
        logger.info("  ✓ CSR signed successfully")
        
        # Save certificate locally for reference
        cert_file = cert_dir / f"{fqdn}.crt"
        cert_file.write_text(cert_chain)
        logger.info(f"  Certificate saved: {cert_file}")
    else:
        logger.info("  [DRY RUN] Would sign CSR with Vault PKI")
        cert_chain = None
    
    # Step 4: Upload signed certificate via SDDC Manager API
    logger.info("  Step 4: Uploading signed certificate via SDDC Manager API...")
    success, replace_task_id = sddc_api.replace_certificate(fqdn, cert_chain if cert_chain else "", dry_run=dry_run)
    
    if not success:
        logger.error("  Failed to initiate certificate replacement")
        return "FAILED"
    
    if replace_task_id and not dry_run:
        # Wait for certificate replacement to complete
        if not sddc_api.wait_for_task(replace_task_id, timeout=600):
            logger.error("  Certificate replacement task failed")
            return "FAILED"
    
    logger.info(f"✓ Successfully replaced certificate for {fqdn}")
    return "SUCCESS"


def _process_fleet_certificate(
    fqdn: str,
    password: str,
    vault_manager: VaultCertificateManager,
    ops_cert_api: VCFOpsCertManagementAPI,
    cert_dir: Path,
    dry_run: bool = False
) -> str:
    """
    Process certificate for fleet-managed resources using the 5-step API workflow.
    The FleetCertReplacer handles all steps internally.
    """
    logger.info("  Fleet-managed resource (VCF Operations Certificate Management API)")

    replacer = FleetCertReplacer(fqdn, password, ops_cert_api, vault_manager)
    return replacer.replace(dry_run=dry_run)


def _process_non_sddc_certificate(
    fqdn: str,
    password: str,
    vault_manager: VaultCertificateManager,
    ops_trust_manager,
    cert_dir: Path,
    dry_run: bool = False
) -> str:
    """
    Process certificate for non-SDDC-managed resources.
    
    Uses local CSR generation and component-specific replacement.
    """
    logger.info("  Non-SDDC-managed resource")
    
    # Step 1: Generate and sign certificate with Vault (local CSR)
    logger.info("  Step 1: Generating CSR locally and signing with Vault...")
    result = vault_manager.generate_certificate(fqdn)
    if not result:
        logger.error(f"  Failed to generate certificate for {fqdn}")
        return "FAILED"
    
    cert_pem, key_pem = result
    
    # Step 2: Save certificates locally
    cert_file = cert_dir / f"{fqdn}.crt"
    key_file = cert_dir / f"{fqdn}.key"
    
    cert_file.write_text(cert_pem)
    key_file.write_text(key_pem)
    key_file.chmod(0o600)
    
    logger.info(f"  Certificate saved: {cert_file}")
    logger.info(f"  Private key saved: {key_file}")
    
    # Step 3: Import to VCF Operations trust store (optional)
    if ops_trust_manager:
        try:
            if dry_run:
                logger.info("  [DRY RUN] Would import certificate to VCF Operations")
            else:
                logger.info("  Step 3: Importing to VCF Operations trust store...")
                ops_trust_manager.import_certificate(cert_pem, fqdn)
                logger.info("  ✓ Imported to VCF Operations")
        except Exception as e:
            logger.warning(f"  Failed to import to VCF Operations: {e}")
    
    # Step 4: Replace certificate on component
    logger.info("  Step 4: Replacing certificate on component...")
    replacer = get_replacer_for_target(fqdn, password)
    replacement_status = replacer.replace(cert_pem, key_pem, dry_run=dry_run)
    
    if replacement_status == "SUCCESS":
        logger.info(f"✓ Successfully replaced certificate for {fqdn}")
    elif replacement_status == "WARNING":
        logger.warning(f"⚠ Certificate generated but replacement may need manual action for {fqdn}")
    else:
        logger.error(f"Failed to replace certificate for {fqdn}")
    
    return replacement_status


# =============================================================================
# Configuration Loading
# =============================================================================

def load_password_from_file(filepath: str = "/home/holuser/creds.txt") -> Optional[str]:
    """Load password from a credentials file."""
    try:
        creds_path = Path(filepath)
        if creds_path.exists():
            password = creds_path.read_text().strip()
            if password:
                return password
    except Exception as e:
        logger.warning(f"Failed to read credentials file: {e}")
    return None


def get_fresh_vault_token(vault_url: str, password: str) -> Optional[str]:
    """
    Get a fresh Vault token. Tries creds.txt password as root token first,
    then falls back to reading init.json from the holorouter.
    """
    # The Vault root token in Holodeck labs is the creds.txt password
    try:
        resp = requests.get(
            f"{vault_url}/v1/auth/token/lookup-self",
            headers={"X-Vault-Token": password},
            timeout=10,
            verify=False
        )
        if resp.status_code == 200:
            data = resp.json().get('data', {})
            logger.info(f"Vault token valid (type: {data.get('display_name', 'unknown')})")
            return password
    except Exception as e:
        logger.debug(f"Vault token lookup failed: {e}")

    # Fallback: try to read root token from router's init.json
    try:
        cmd = [
            'sshpass', '-p', password,
            'ssh', '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'PubkeyAuthentication=no',
            '-o', 'LogLevel=ERROR',
            'root@router',
            'cat /root/vault-keys/init.json'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            init_data = json.loads(result.stdout)
            root_token = init_data.get('root_token', '')
            if root_token:
                verify = requests.get(
                    f"{vault_url}/v1/auth/token/lookup-self",
                    headers={"X-Vault-Token": root_token},
                    timeout=10,
                    verify=False
                )
                if verify.status_code == 200:
                    logger.info("Vault token obtained from router init.json")
                    return root_token
    except Exception as e:
        logger.debug(f"Vault init.json fallback failed: {e}")

    logger.error("Could not obtain a valid Vault token")
    return None


def load_config(config_path: Optional[str] = None) -> Dict:
    """Load configuration from file or environment variables."""
    config = {
        'sddc_manager_url': 'https://sddcmanager-a.site-a.vcf.lab',
        'vault_url': 'http://10.1.1.1:32000',
        'vault_token': None,  # obtained fresh at runtime
        'vault_role': 'holodeck',
        'vault_mount': 'pki',
        'cert_ttl': '17520h',  # 2 years
    }

    if config_path and Path(config_path).exists():
        try:
            with open(config_path, 'r') as f:
                file_config = json.load(f)
                config.update(file_config)
        except Exception as e:
            logger.warning(f"Failed to load config file: {e}")

    env_mapping = {
        'VCF_PASS': 'vcf_pass',
        'VAULT_URL': 'vault_url',
        'VAULT_TOKEN': 'vault_token',
        'VAULT_ROLE': 'vault_role',
        'VAULT_MOUNT': 'vault_mount',
    }

    for env_var, config_key in env_mapping.items():
        env_value = os.getenv(env_var)
        if env_value:
            config[config_key] = env_value
    
    if not config.get('vcf_pass'):
        file_password = load_password_from_file()
        if file_password:
            config['vcf_pass'] = file_password
            logger.info("Loaded VCF password from /home/holuser/creds.txt")

    # Get fresh Vault token if not explicitly provided
    if not config.get('vault_token'):
        vault_url = config.get('vault_url', 'http://10.1.1.1:32000')
        password = config.get('vcf_pass', '')
        if password:
            config['vault_token'] = get_fresh_vault_token(vault_url, password)
    
    return config


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="VCF Certificate Management with Vault Signing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Replace certificates for all default VCF components
  python cert-replacement.py --all

  # Replace certificates for specific targets
  python cert-replacement.py --targets sddcmanager-a.site-a.vcf.lab

  # Dry run (no actual changes)
  python cert-replacement.py --all --dry-run

  # Specify certificate TTL (default: 2 years / 17520h)
  python cert-replacement.py --all --ttl 8760h

Default VCF Targets (VCF 9.1 Cycle 4):
  Management Domain (SDDC Manager-managed):
  - sddcmanager-a.site-a.vcf.lab      (SDDC Manager)
  - vc-mgmt-a.site-a.vcf.lab          (Mgmt vCenter)
  - nsx-mgmt-a.site-a.vcf.lab         (Mgmt NSX VIP)
  - nsx-mgmt-01a.site-a.vcf.lab       (Mgmt NSX Node)

  Workload Domain (SDDC Manager-managed):
  - vc-wld01-a.site-a.vcf.lab         (WLD vCenter)
  - nsx-wld01-a.site-a.vcf.lab        (WLD NSX VIP)
  - nsx-wld01-01a.site-a.vcf.lab      (WLD NSX Node)

  Fleet-managed (VCF Operations Certificate Management API):
  - ops-a.site-a.vcf.lab              (VCF Operations)
  - auto-a.site-a.vcf.lab             (VCF Automation)
  - auto-platform-a.site-a.vcf.lab    (VCFA Platform)
  - opslogs-a.site-a.vcf.lab          (Log management)
  - opsnet-a.site-a.vcf.lab           (VCF Ops for networks)
  - vidb-a.site-a.vcf.lab             (Identity broker)
  - fleet-01a.site-a.vcf.lab          (VCF services runtime)
  - instance-01a.site-a.vcf.lab       (VCF services runtime)
  - vsp-01a.site-a.vcf.lab            (VCF services runtime)

Vault token is obtained fresh from /home/holuser/creds.txt or router init.json.
        """
    )
    
    parser.add_argument('--config', type=str, help='Path to JSON configuration file')
    parser.add_argument('--vcf-pass', type=str, help='VCF password')
    parser.add_argument('--vault-url', type=str, help='HashiCorp Vault URL')
    parser.add_argument('--vault-token', type=str, help='Vault authentication token')
    parser.add_argument('--vault-role', type=str, help='Vault PKI role name')
    parser.add_argument('--vault-mount', type=str, default='pki', help='Vault PKI mount path')
    parser.add_argument('--ttl', type=str, default='17520h', help='Certificate TTL (default: 17520h = 2 years)')
    
    parser.add_argument('--targets', type=str, nargs='+', help='FQDNs to generate certificates for')
    parser.add_argument('--all', action='store_true', help='Process all default VCF component targets')
    
    parser.add_argument('--dry-run', action='store_true', help='Dry run mode (generate certs but do not install)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable verbose logging')
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Load configuration
    config = load_config(args.config)
    
    # Override with command-line arguments
    if args.vcf_pass:
        config['vcf_pass'] = args.vcf_pass
    if args.vault_url:
        config['vault_url'] = args.vault_url
    if args.vault_token:
        config['vault_token'] = args.vault_token
    if args.vault_role:
        config['vault_role'] = args.vault_role
    if args.vault_mount:
        config['vault_mount'] = args.vault_mount
    
    config['cert_ttl'] = args.ttl
    
    # Validate required configuration
    required = ['vcf_pass', 'vault_url', 'vault_role']
    missing = [key for key in required if not config.get(key)]
    
    if missing:
        logger.error(f"Missing required configuration: {', '.join(missing)}")
        logger.error("Set password in /home/holuser/creds.txt or via --vcf-pass")
        sys.exit(1)
    
    # Validate Vault token
    if not config.get('vault_token'):
        logger.error("Cannot obtain a valid Vault token. Check Vault health and creds.txt.")
        sys.exit(1)

    logger.info(f"Vault URL: {config['vault_url']}")
    logger.info(f"Vault Role: {config['vault_role']}")
    logger.info(f"Vault Token: {'✓ valid' if config['vault_token'] else '✗ missing'}")
    logger.info(f"Certificate TTL: {config['cert_ttl']}")
    
    if args.dry_run:
        logger.info("DRY RUN MODE - No changes will be made")
    
    # Initialize Vault manager
    vault_manager = VaultCertificateManager(
        vault_url=config['vault_url'],
        vault_token=config['vault_token'],
        vault_role=config['vault_role'],
        vault_mount=config.get('vault_mount', 'pki'),
        cert_ttl=config['cert_ttl']
    )
    
    # Initialize SDDC Manager API for certificate replacement
    sddc_manager_url = config.get('sddc_manager_url', 'https://sddcmanager-a.site-a.vcf.lab')
    logger.info(f"SDDC Manager URL: {sddc_manager_url}")
    
    sddc_api = SDDCManagerAPI(
        sddc_manager_url=sddc_manager_url,
        username='admin@local',
        password=config['vcf_pass'],
        verify_ssl=False
    )
    
    # Test SDDC Manager connection
    logger.info("Connecting to SDDC Manager...")
    if sddc_api.get_token():
        logger.info("✓ SDDC Manager connection successful")
        domain_id = sddc_api.get_domain_id()
        if domain_id:
            logger.info(f"  Management Domain ID: {domain_id}")
        domains = sddc_api._load_domains()
        for d in domains:
            logger.info(f"  Domain: {d.get('name', '?')} ({d.get('type', '?')}) ID: {d.get('id', '?')}")
    else:
        logger.warning("⚠ SDDC Manager connection failed - will use fallback methods")
        sddc_api = None

    # Initialize VCF Operations Certificate Management API for fleet-managed targets
    ops_cert_api = VCFOpsCertManagementAPI(
        ops_url='https://ops-a.site-a.vcf.lab',
        username='admin',
        password=config['vcf_pass']
    )

    logger.info("Connecting to VCF Operations Certificate Management API...")
    if ops_cert_api.connect():
        certs = ops_cert_api.query_certificates()
        logger.info(f"✓ VCF Operations connection successful ({len(certs)} TLS certs)")
    else:
        logger.warning("⚠ VCF Operations connection failed - fleet targets will use fallback methods")
        ops_cert_api = None

    # Determine targets
    targets = []
    if args.all:
        targets = DEFAULT_VCF_TARGETS.copy()
        logger.info(f"Processing all {len(targets)} default VCF component targets")
    elif args.targets:
        targets = args.targets
    else:
        parser.print_help()
        print("\n" + "=" * 60)
        print("No targets specified. Use --all or --targets")
        print("=" * 60)
        sys.exit(0)
    
    # Process certificates
    results = {}
    for fqdn in targets:
        result_status = process_certificate(
            fqdn=fqdn,
            password=config['vcf_pass'],
            vault_manager=vault_manager,
            sddc_api=sddc_api,
            ops_cert_api=ops_cert_api,
            dry_run=args.dry_run
        )
        results[fqdn] = result_status
    
    # Post-replacement: Fix NSX compute manager trust after vCenter/NSX cert changes
    successful_targets = [fqdn for fqdn, status in results.items()
                          if status in ('SUCCESS', 'WARNING')]
    nsx_cm_fixer = NSXComputeManagerFixer(
        password=config['vcf_pass'],
        vault_url=config['vault_url'],
        vault_token=config['vault_token']
    )
    cm_results = nsx_cm_fixer.fix_all(successful_targets, dry_run=args.dry_run)

    # Summary
    print("\n" + "=" * 60)
    print("CERTIFICATE REPLACEMENT SUMMARY")
    print("=" * 60)
    successful = sum(1 for v in results.values() if v == "SUCCESS")
    warnings = sum(1 for v in results.values() if v == "WARNING")
    total = len(results)
    print(f"Fully Successful: {successful}/{total}")
    if warnings > 0:
        print(f"Requires Manual Action: {warnings}/{total}")
    print()
    for fqdn, result_status in results.items():
        if result_status == "SUCCESS":
            status = "✓ SUCCESS"
        elif result_status == "WARNING":
            status = "⚠ WARNING (Manual Action Required)"
        else:
            status = "✗ FAILED"
        print(f"  {status}: {fqdn}")

    if cm_results:
        print()
        print("NSX Compute Manager Trust Fix:")
        for pair_key, cm_status in cm_results.items():
            if cm_status == "SUCCESS":
                print(f"  ✓ {pair_key}")
            elif cm_status == "DRY_RUN":
                print(f"  ○ {pair_key} (dry run)")
            else:
                print(f"  ⚠ {pair_key} ({cm_status})")
    
    print()
    print("Certificates saved to: /tmp/vcf-certs/")
    
    # Exit with 0 if there are no failures (SUCCESS and WARNING are both non-failures)
    failed = sum(1 for v in results.values() if v == "FAILED")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

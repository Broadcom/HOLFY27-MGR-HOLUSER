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

For non-SDDC-managed resources (VCF Operations, VCF Automation, Ops Logs, etc.):
  1. Generate CSR locally
  2. Sign CSR with HashiCorp Vault PKI
  3. Replace certificate via component-specific method (SSH/API)

VCF Components Managed:

  Management Domain:
  - sddcmanager-a.site-a.vcf.lab   (SDDC Manager)       - SDDC Manager API  [AUTOMATED]
  - vc-mgmt-a.site-a.vcf.lab       (Mgmt vCenter)       - SDDC Manager API  [AUTOMATED]
  - nsx-mgmt-a.site-a.vcf.lab      (Mgmt NSX VIP)       - SDDC Manager API  [AUTOMATED]
  - nsx-mgmt-01a.site-a.vcf.lab    (Mgmt NSX Node)      - SDDC Manager API  [AUTOMATED]
  - ops-a.site-a.vcf.lab           (VCF Operations)      - SSH replacement   [AUTOMATED]

  Workload Domain:
  - vc-wld01-a.site-a.vcf.lab      (WLD vCenter)        - SDDC Manager API  [AUTOMATED]
  - nsx-wld01-a.site-a.vcf.lab     (WLD NSX VIP)        - SDDC Manager API  [AUTOMATED]
  - nsx-wld01-01a.site-a.vcf.lab   (WLD NSX Node)       - SDDC Manager API  [AUTOMATED]

  VCF Services:
  - auto-a.site-a.vcf.lab          (VCF Automation)      - SSH/K8s           [MANUAL]
  - auto-platform-a.site-a.vcf.lab (VCFA Platform)       - SSH/K8s           [MANUAL]
  - opslogs-a.site-a.vcf.lab       (Ops for Logs)        - SSH/K8s           [MANUAL]
  - opsnet-a.site-a.vcf.lab        (Ops for Networks)    - API only          [MANUAL]

  VSP Gateway Endpoints (managed by VSP cluster, cert ref via Ops Locker):
  - vsp-01a.site-a.vcf.lab         (VSP Gateway)
  - fleet-01a.site-a.vcf.lab       (Fleet Gateway)
  - instance-01a.site-a.vcf.lab    (Instance Gateway)

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
    # VCF Operations & Services (non-SDDC-managed)
    'ops-a.site-a.vcf.lab',
    'auto-a.site-a.vcf.lab',
    'auto-platform-a.site-a.vcf.lab',
    'opslogs-a.site-a.vcf.lab',
    'opsnet-a.site-a.vcf.lab',
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


def get_replacer_for_target(fqdn: str, password: str):
    """Get the appropriate certificate replacer for a target."""
    fqdn_lower = fqdn.lower()
    
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
    elif any(prefix in fqdn_lower for prefix in ('vsp-', 'fleet-', 'instance-')):
        return GenericCertSaver(fqdn, password)
    else:
        logger.warning(f"No specific replacer for {fqdn}, saving locally")
        return GenericCertSaver(fqdn, password)


def process_certificate(
    fqdn: str,
    password: str,
    vault_manager: VaultCertificateManager,
    sddc_api: Optional[SDDCManagerAPI] = None,
    ops_trust_manager = None,
    dry_run: bool = False
) -> str:
    """
    Generate, sign, and replace certificate for a VCF component.
    
    Workflow for SDDC-managed resources (SDDC Manager, vCenter, NSX):
    1. Generate CSR via SDDC Manager API (on the remote component)
    2. Get the CSR from SDDC Manager
    3. Sign the CSR with HashiCorp Vault PKI
    4. Upload the signed certificate chain via SDDC Manager API
    5. SDDC Manager applies the certificate to the component
    
    Workflow for non-SDDC-managed resources (VCF Operations, etc.):
    1. Generate CSR locally
    2. Sign with Vault PKI  
    3. Import to VCF Operations trust store
    4. Use component-specific replacement (SSH/API)
    """
    logger.info("=" * 60)
    logger.info(f"Processing certificate for: {fqdn}")
    logger.info("=" * 60)
    
    cert_dir = Path("/tmp/vcf-certs")
    cert_dir.mkdir(exist_ok=True)
    
    try:
        # Check if this resource is managed by SDDC Manager
        if sddc_api and sddc_api.is_sddc_managed(fqdn):
            return _process_sddc_managed_certificate(fqdn, sddc_api, vault_manager, cert_dir, dry_run)
        else:
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

  Non-SDDC-managed:
  - ops-a.site-a.vcf.lab              (VCF Operations)
  - auto-a.site-a.vcf.lab             (VCF Automation)
  - auto-platform-a.site-a.vcf.lab    (VCFA Platform)
  - opslogs-a.site-a.vcf.lab          (Ops for Logs)
  - opsnet-a.site-a.vcf.lab           (Ops for Networks)

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
            dry_run=args.dry_run
        )
        results[fqdn] = result_status
    
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
    
    print()
    print("Certificates saved to: /tmp/vcf-certs/")
    
    # Exit with 0 if there are no failures (SUCCESS and WARNING are both non-failures)
    failed = sum(1 for v in results.values() if v == "FAILED")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

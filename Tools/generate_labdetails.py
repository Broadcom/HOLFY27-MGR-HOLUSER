#!/usr/bin/env python3
"""
generate_labdetails.py - Automatic Lab Documentation Generator
Version 1.0 - February 2026
Author - HOL Core Team

Generates a comprehensive LABDETAILS.md file with Mermaid diagrams
by querying live vCenter, NSX, and SDDC Manager environments.

Usage:
    python3 Tools/generate_labdetails.py
    python3 Tools/generate_labdetails.py --output /path/to/LABDETAILS.md
    python3 Tools/generate_labdetails.py --dry-run
"""

import os
import sys
import json
import socket
import argparse
import datetime
import subprocess
from configparser import ConfigParser
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import urllib3

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Try to import pyVmomi
try:
    from pyVim import connect
    from pyVmomi import vim
    PYVMOMI_AVAILABLE = True
except ImportError:
    PYVMOMI_AVAILABLE = False
    print("WARNING: pyVmomi not available. vCenter queries will be limited.")

#==============================================================================
# CONFIGURATION
#==============================================================================

# Paths
HOME = '/home/holuser'
HOL_ROOT = f'{HOME}/hol'
CONFIG_INI = '/tmp/config.ini'
CREDS_FILE = f'{HOME}/creds.txt'
DEFAULT_OUTPUT = f'{HOL_ROOT}/LABDETAILS.md'

# Mermaid color styles for different sections
# Using CSS-style colors in Mermaid style definitions
MERMAID_STYLES = """
    %% Color Styles
    classDef coreVM fill:#d4edda,stroke:#28a745,stroke-width:2px,color:#155724
    classDef mgmtDomain fill:#cce5ff,stroke:#004085,stroke-width:2px,color:#004085
    classDef wldDomain fill:#ffe5cc,stroke:#fd7e14,stroke-width:2px,color:#856404
    classDef external fill:#f8d7da,stroke:#721c24,stroke-width:2px,color:#721c24
    classDef aria fill:#e2d5f1,stroke:#6f42c1,stroke-width:2px,color:#4a2c7a
    classDef storage fill:#fff3cd,stroke:#856404,stroke-width:2px,color:#856404
    classDef network fill:#d1ecf1,stroke:#0c5460,stroke-width:2px,color:#0c5460
"""

#==============================================================================
# DATA CLASSES
#==============================================================================

@dataclass
class VMInfo:
    """Virtual Machine information"""
    name: str
    power_state: str
    vcpus: int = 0
    memory_mb: int = 0
    ip_address: str = ""
    host: str = ""
    description: str = ""

@dataclass
class HostInfo:
    """ESXi Host information"""
    fqdn: str
    state: str
    power_state: str
    cpu_cores: int = 0
    memory_gb: float = 0
    mgmt_ip: str = ""
    vsan_ip: str = ""
    vmotion_ip: str = ""
    cluster: str = ""
    domain: str = ""

@dataclass
class ClusterInfo:
    """Cluster information"""
    name: str
    host_count: int = 0
    total_cpu_mhz: int = 0
    total_memory_gb: float = 0
    datastore: str = ""
    datastore_type: str = ""
    domain: str = ""

@dataclass
class DatastoreInfo:
    """Datastore information"""
    name: str
    ds_type: str
    capacity_gb: float = 0
    free_gb: float = 0

@dataclass
class DomainInfo:
    """VCF Domain information"""
    name: str
    domain_type: str
    vcenter_fqdn: str = ""
    nsx_fqdn: str = ""
    sso_domain: str = ""
    clusters: List[str] = field(default_factory=list)

@dataclass
class NetworkInfo:
    """Network/Portgroup information"""
    name: str
    dvs_name: str = ""
    vlan: str = ""

@dataclass 
class NSXEdgeInfo:
    """NSX Edge information"""
    name: str
    mgmt_ip: str = ""
    tep_ips: List[str] = field(default_factory=list)
    cluster: str = ""

@dataclass
class LabEnvironment:
    """Complete lab environment data"""
    lab_sku: str = ""
    lab_type: str = ""
    vcf_version: str = ""
    esxi_version: str = ""
    dns_domain: str = ""
    
    # Core VMs
    router_ip: str = ""
    console_ip: str = ""
    manager_ip: str = ""
    
    # Domains
    domains: List[DomainInfo] = field(default_factory=list)
    
    # Clusters
    clusters: List[ClusterInfo] = field(default_factory=list)
    
    # Hosts
    hosts: List[HostInfo] = field(default_factory=list)
    
    # VMs
    mgmt_vms: List[VMInfo] = field(default_factory=list)
    wld_vms: List[VMInfo] = field(default_factory=list)
    
    # Datastores
    datastores: List[DatastoreInfo] = field(default_factory=list)
    
    # Networks
    mgmt_networks: List[NetworkInfo] = field(default_factory=list)
    wld_networks: List[NetworkInfo] = field(default_factory=list)
    
    # NSX
    nsx_edges: List[NSXEdgeInfo] = field(default_factory=list)
    
    # URLs
    urls: List[tuple] = field(default_factory=list)

#==============================================================================
# UTILITY FUNCTIONS
#==============================================================================

def get_password() -> str:
    """Read password from creds.txt"""
    if os.path.isfile(CREDS_FILE):
        with open(CREDS_FILE, 'r') as f:
            return f.read().strip()
    return ""

def test_ping(host: str, timeout: int = 2) -> bool:
    """Test if host is reachable via ping"""
    try:
        result = subprocess.run(
            ['ping', '-c', '1', '-W', str(timeout), host],
            capture_output=True,
            timeout=timeout + 1
        )
        return result.returncode == 0
    except Exception:
        return False

def resolve_host(hostname: str) -> str:
    """Resolve hostname to IP address"""
    try:
        return socket.gethostbyname(hostname)
    except Exception:
        return ""

def safe_api_call(func, *args, **kwargs) -> Optional[Any]:
    """Safely execute an API call and return None on failure"""
    try:
        return func(*args, **kwargs)
    except Exception as e:
        print(f"  API call failed: {e}")
        return None

#==============================================================================
# DATA COLLECTION
#==============================================================================

class LabDataCollector:
    """Collects lab environment data from various sources"""
    
    def __init__(self, config_path: str = CONFIG_INI):
        self.config = ConfigParser()
        self.config_path = config_path
        self.password = get_password()
        self.env = LabEnvironment()
        self.vcenter_connections = {}
        
    def collect_all(self) -> LabEnvironment:
        """Collect all lab environment data"""
        print("Starting lab data collection...")
        
        # Load config
        self._load_config()
        
        # Collect core infrastructure info
        self._collect_core_info()
        
        # Collect from SDDC Manager
        self._collect_sddc_info()
        
        # Collect from vCenters
        self._collect_vcenter_info()
        
        # Collect NSX info
        self._collect_nsx_info()
        
        # Disconnect vCenters
        self._disconnect_vcenters()
        
        print("Data collection complete.")
        return self.env
    
    def _load_config(self):
        """Load configuration from config.ini"""
        print("Loading configuration...")
        
        if not os.path.isfile(self.config_path):
            print(f"  Config file not found: {self.config_path}")
            return
        
        self.config.read(self.config_path)
        
        # Extract lab info
        if self.config.has_option('VPOD', 'vPod_SKU'):
            self.env.lab_sku = self.config.get('VPOD', 'vPod_SKU')
        
        if self.config.has_option('VPOD', 'labtype'):
            self.env.lab_type = self.config.get('VPOD', 'labtype')
        
        # Get DNS domain from resolv.conf
        try:
            with open('/etc/resolv.conf', 'r') as f:
                for line in f:
                    if line.startswith('search'):
                        domains = line.split()[1:]
                        if domains:
                            self.env.dns_domain = domains[0]
                        break
        except Exception:
            self.env.dns_domain = "site-a.vcf.lab"
        
        # Collect URLs from config
        if self.config.has_option('RESOURCES', 'URLS'):
            urls_raw = self.config.get('RESOURCES', 'URLS')
            for line in urls_raw.strip().split('\n'):
                line = line.strip()
                if line and not line.startswith('#'):
                    parts = line.split(',', 1)
                    url = parts[0].strip()
                    text = parts[1].strip() if len(parts) > 1 else ""
                    self.env.urls.append((url, text))
        
        print(f"  Lab SKU: {self.env.lab_sku}")
        print(f"  Lab Type: {self.env.lab_type}")
    
    def _collect_core_info(self):
        """Collect core infrastructure information"""
        print("Collecting core infrastructure info...")
        
        # Router
        router_ip = resolve_host('router')
        if router_ip:
            self.env.router_ip = router_ip
        else:
            self.env.router_ip = "10.1.10.129"
        
        # Console
        console_ip = resolve_host('console')
        if console_ip:
            self.env.console_ip = console_ip
        else:
            self.env.console_ip = "10.1.10.130"
        
        # Manager (this machine)
        try:
            result = subprocess.run(['hostname', '-I'], capture_output=True, text=True)
            if result.returncode == 0:
                self.env.manager_ip = result.stdout.strip().split()[0]
        except Exception:
            self.env.manager_ip = "10.1.10.131"
        
        print(f"  Router: {self.env.router_ip}")
        print(f"  Console: {self.env.console_ip}")
        print(f"  Manager: {self.env.manager_ip}")
    
    def _collect_sddc_info(self):
        """Collect information from SDDC Manager"""
        print("Collecting SDDC Manager info...")
        
        sddc_host = "sddcmanager-a.site-a.vcf.lab"
        
        # Get access token
        token = self._get_sddc_token(sddc_host)
        if not token:
            print("  Could not authenticate to SDDC Manager")
            return
        
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
        
        # Get domains
        try:
            resp = requests.get(
                f'https://{sddc_host}/v1/domains',
                headers=headers,
                verify=False,
                timeout=30
            )
            if resp.status_code == 200:
                data = resp.json()
                for elem in data.get('elements', []):
                    domain = DomainInfo(
                        name=elem.get('name', ''),
                        domain_type=elem.get('type', ''),
                        sso_domain=elem.get('ssoName', '')
                    )
                    
                    # Get vCenter
                    vcenters = elem.get('vcenters', [])
                    if vcenters:
                        domain.vcenter_fqdn = vcenters[0].get('fqdn', '')
                    
                    # Get NSX
                    nsx = elem.get('nsxtCluster', {})
                    if nsx:
                        domain.nsx_fqdn = nsx.get('vipFqdn', '')
                    
                    # Get clusters
                    for cl in elem.get('clusters', []):
                        domain.clusters.append(cl.get('id', ''))
                    
                    self.env.domains.append(domain)
                    print(f"  Found domain: {domain.name} ({domain.domain_type})")
        except Exception as e:
            print(f"  Error getting domains: {e}")
        
        # Get clusters
        try:
            resp = requests.get(
                f'https://{sddc_host}/v1/clusters',
                headers=headers,
                verify=False,
                timeout=30
            )
            if resp.status_code == 200:
                data = resp.json()
                for elem in data.get('elements', []):
                    cluster = ClusterInfo(
                        name=elem.get('name', ''),
                        host_count=len(elem.get('hosts', [])),
                        datastore=elem.get('primaryDatastoreName', ''),
                        datastore_type=elem.get('primaryDatastoreType', '')
                    )
                    
                    # Find domain for this cluster
                    for domain in self.env.domains:
                        if elem.get('domain', {}).get('id') in str(domain.clusters):
                            cluster.domain = domain.name
                            break
                    
                    self.env.clusters.append(cluster)
                    print(f"  Found cluster: {cluster.name}")
        except Exception as e:
            print(f"  Error getting clusters: {e}")
        
        # Get hosts
        try:
            resp = requests.get(
                f'https://{sddc_host}/v1/hosts',
                headers=headers,
                verify=False,
                timeout=30
            )
            if resp.status_code == 200:
                data = resp.json()
                for elem in data.get('elements', []):
                    host = HostInfo(
                        fqdn=elem.get('fqdn', ''),
                        state=elem.get('status', ''),
                        power_state='poweredOn',
                        cpu_cores=elem.get('cpu', {}).get('cores', 0),
                        memory_gb=elem.get('memory', {}).get('totalCapacityMB', 0) / 1024
                    )
                    
                    # Get ESXi version
                    if not self.env.esxi_version and elem.get('esxiVersion'):
                        self.env.esxi_version = elem.get('esxiVersion')
                    
                    # Get IP addresses
                    for ip_info in elem.get('ipAddresses', []):
                        ip_type = ip_info.get('type', '')
                        ip_addr = ip_info.get('ipAddress', '')
                        if ip_type == 'VSAN':
                            host.vsan_ip = ip_addr
                        elif ip_type == 'VMOTION':
                            host.vmotion_ip = ip_addr
                    
                    # Get management IP from FQDN
                    host.mgmt_ip = resolve_host(host.fqdn)
                    
                    self.env.hosts.append(host)
                    print(f"  Found host: {host.fqdn}")
        except Exception as e:
            print(f"  Error getting hosts: {e}")
    
    def _get_sddc_token(self, host: str) -> Optional[str]:
        """Get SDDC Manager access token"""
        try:
            resp = requests.post(
                f'https://{host}/v1/tokens',
                json={
                    'username': 'administrator@vsphere.local',
                    'password': self.password
                },
                verify=False,
                timeout=30
            )
            if resp.status_code == 200:
                return resp.json().get('accessToken')
        except Exception as e:
            print(f"  Token request failed: {e}")
        return None
    
    def _collect_vcenter_info(self):
        """Collect information from vCenter servers"""
        if not PYVMOMI_AVAILABLE:
            print("pyVmomi not available, skipping vCenter collection")
            return
        
        print("Collecting vCenter info...")
        
        # Connect to each domain's vCenter
        for domain in self.env.domains:
            if not domain.vcenter_fqdn:
                continue
            
            print(f"  Connecting to {domain.vcenter_fqdn}...")
            
            # Determine user based on SSO domain
            if domain.sso_domain == 'vsphere.local':
                user = 'administrator@vsphere.local'
            else:
                user = f'administrator@{domain.sso_domain}'
            
            si = self._connect_vcenter(domain.vcenter_fqdn, user)
            if not si:
                continue
            
            self.vcenter_connections[domain.vcenter_fqdn] = si
            content = si.RetrieveContent()
            
            # Get VMs
            vms_list = self.env.mgmt_vms if domain.domain_type == 'MANAGEMENT' else self.env.wld_vms
            
            container = content.viewManager.CreateContainerView(
                content.rootFolder, [vim.VirtualMachine], True
            )
            for vm in container.view:
                vm_info = VMInfo(
                    name=vm.name,
                    power_state=str(vm.runtime.powerState),
                    vcpus=vm.summary.config.numCpu if hasattr(vm.summary.config, 'numCpu') else 0,
                    memory_mb=vm.summary.config.memorySizeMB if hasattr(vm.summary.config, 'memorySizeMB') else 0,
                    ip_address=vm.guest.ipAddress if vm.guest and vm.guest.ipAddress else ""
                )
                vms_list.append(vm_info)
            container.Destroy()
            
            print(f"    Found {len(vms_list)} VMs")
            
            # Get Datastores
            container = content.viewManager.CreateContainerView(
                content.rootFolder, [vim.Datastore], True
            )
            for ds in container.view:
                # Avoid duplicates
                if not any(d.name == ds.name for d in self.env.datastores):
                    ds_info = DatastoreInfo(
                        name=ds.name,
                        ds_type=ds.summary.type,
                        capacity_gb=ds.summary.capacity / (1024**3),
                        free_gb=ds.summary.freeSpace / (1024**3)
                    )
                    self.env.datastores.append(ds_info)
            container.Destroy()
            
            # Get Networks/Port Groups
            networks_list = self.env.mgmt_networks if domain.domain_type == 'MANAGEMENT' else self.env.wld_networks
            
            container = content.viewManager.CreateContainerView(
                content.rootFolder, [vim.dvs.DistributedVirtualPortgroup], True
            )
            for pg in container.view:
                net_info = NetworkInfo(
                    name=pg.name,
                    dvs_name=pg.config.distributedVirtualSwitch.name if pg.config.distributedVirtualSwitch else ""
                )
                networks_list.append(net_info)
            container.Destroy()
            
            # Update cluster info with vCenter data
            container = content.viewManager.CreateContainerView(
                content.rootFolder, [vim.ClusterComputeResource], True
            )
            for cluster in container.view:
                for cl_info in self.env.clusters:
                    if cl_info.name == cluster.name:
                        cl_info.total_cpu_mhz = cluster.summary.totalCpu
                        cl_info.total_memory_gb = cluster.summary.totalMemory / (1024**3)
                        cl_info.domain = domain.name
            container.Destroy()
    
    def _connect_vcenter(self, host: str, user: str) -> Optional[Any]:
        """Connect to a vCenter server"""
        try:
            # Try pyVmomi 8.0+ method first
            try:
                si = connect.SmartConnect(
                    host=host,
                    user=user,
                    pwd=self.password,
                    disableSslCertValidation=True
                )
            except TypeError:
                # Fallback for older pyVmomi
                si = connect.SmartConnectNoSSL(
                    host=host,
                    user=user,
                    pwd=self.password
                )
            return si
        except Exception as e:
            print(f"    Connection failed: {e}")
            return None
    
    def _disconnect_vcenters(self):
        """Disconnect all vCenter connections"""
        for host, si in self.vcenter_connections.items():
            try:
                connect.Disconnect(si)
            except Exception:
                pass
    
    def _collect_nsx_info(self):
        """Collect NSX Edge information"""
        print("Collecting NSX info...")
        
        for domain in self.env.domains:
            if not domain.nsx_fqdn:
                continue
            
            # Get the NSX manager node (not VIP)
            nsx_node = domain.nsx_fqdn.replace('nsx-mgmt-a', 'nsx-mgmt-01a').replace('nsx-wld01-a', 'nsx-wld01-01a')
            
            print(f"  Querying {nsx_node}...")
            
            try:
                resp = requests.get(
                    f'https://{nsx_node}/api/v1/transport-nodes?node_types=EdgeNode',
                    auth=('admin', self.password),
                    verify=False,
                    timeout=30
                )
                
                if resp.status_code == 200:
                    data = resp.json()
                    for elem in data.get('results', []):
                        node_info = elem.get('node_deployment_info', {})
                        
                        edge = NSXEdgeInfo(
                            name=node_info.get('display_name', elem.get('display_name', '')),
                            cluster=domain.name
                        )
                        
                        # Get management IP
                        ip_list = node_info.get('ip_addresses', [])
                        if ip_list:
                            edge.mgmt_ip = ip_list[0]
                        
                        # Get TEP IPs
                        host_switches = elem.get('host_switch_spec', {}).get('host_switches', [])
                        for hs in host_switches:
                            ip_spec = hs.get('ip_assignment_spec', {})
                            edge.tep_ips = ip_spec.get('ip_list', [])
                        
                        self.env.nsx_edges.append(edge)
                        print(f"    Found edge: {edge.name}")
            except Exception as e:
                print(f"    Error querying NSX: {e}")

#==============================================================================
# MARKDOWN GENERATOR
#==============================================================================

class LabDetailsGenerator:
    """Generates LABDETAILS.md from collected environment data"""
    
    def __init__(self, env: LabEnvironment):
        self.env = env
        self.lines = []
    
    def generate(self) -> str:
        """Generate the complete LABDETAILS.md content"""
        self._add_header()
        self._add_high_level_architecture()
        self._add_network_architecture()
        self._add_vcf_domain_architecture()
        self._add_esxi_host_layout()
        self._add_vm_inventory()
        self._add_core_infrastructure()
        self._add_network_subnets()
        self._add_dvs_diagrams()
        self._add_nsx_architecture()
        self._add_boot_sequence()
        self._add_web_interfaces()
        self._add_credentials()
        self._add_storage_summary()
        self._add_complete_diagram()
        self._add_quick_reference()
        self._add_footer()
        
        return '\n'.join(self.lines)
    
    def _add(self, line: str = ""):
        """Add a line to the output"""
        self.lines.append(line)
    
    def _add_header(self):
        """Add document header"""
        lab_type_desc = {
            'HOL': 'Hands-on Labs',
            'ATE': 'Advanced Technical Enablement / Livefire',
            'VXP': 'VCF Experience Program',
            'EDU': 'Education/Training',
            'Discovery': 'Discovery Environment'
        }
        
        type_desc = lab_type_desc.get(self.env.lab_type, self.env.lab_type)
        
        self._add(f"# {self.env.lab_sku} - Lab Environment Documentation")
        self._add()
        self._add("## Lab Overview")
        self._add()
        self._add("| Property | Value |")
        self._add("| -------- | ----- |")
        self._add(f"| **Lab SKU** | {self.env.lab_sku} |")
        self._add(f"| **Lab Type** | {self.env.lab_type} ({type_desc}) |")
        
        if self.env.esxi_version:
            # Try to extract VCF version from ESXi version
            vcf_version = "9.0.1" if "9.0" in self.env.esxi_version else "Unknown"
            self._add(f"| **VCF Version** | {vcf_version} |")
            self._add(f"| **ESXi Version** | {self.env.esxi_version} |")
        
        site_count = len(set(d.name.split('-')[-1] if '-' in d.name else 'a' for d in self.env.domains))
        config = "Single Site" if site_count == 1 else f"Multi-Site ({site_count} sites)"
        self._add(f"| **Configuration** | {config} |")
        self._add(f"| **DNS Domain** | {self.env.dns_domain} |")
        self._add(f"| **Credentials** | See `/home/holuser/creds.txt` |")
        self._add()
        self._add("---")
        self._add()
    
    def _add_high_level_architecture(self):
        """Add high-level architecture diagram"""
        self._add("## High-Level Architecture")
        self._add()
        self._add("```mermaid")
        self._add("flowchart TB")
        self._add(MERMAID_STYLES)
        self._add()
        self._add('    subgraph External["External Network"]')
        self._add('        Internet[("Internet<br/>192.168.0.0/24")]')
        self._add('    end')
        self._add()
        self._add('    subgraph vPod["vPod Environment"]')
        self._add('        subgraph CoreVMs["Core Infrastructure VMs<br/>10.1.10.128/25"]')
        self._add(f'            Router["holorouter<br/>{self.env.router_ip}<br/>(DNS/DHCP/Proxy/FW)"]')
        self._add(f'            Console["console<br/>{self.env.console_ip}<br/>(Linux Main Console)"]')
        self._add(f'            Manager["manager<br/>{self.env.manager_ip}<br/>(Lab Startup/Automation)"]')
        self._add('        end')
        self._add()
        self._add('        subgraph VCF["VMware Cloud Foundation"]')
        
        # Add domains
        for domain in self.env.domains:
            domain_id = domain.name.replace('-', '_').replace('.', '_')
            domain_label = "Management Domain" if domain.domain_type == "MANAGEMENT" else f"Workload Domain"
            
            self._add(f'            subgraph {domain_id}["{domain_label} ({domain.name})"]')
            
            if domain.domain_type == "MANAGEMENT":
                self._add(f'                SDDC["SDDC Manager<br/>sddcmanager-a<br/>"]')
            
            vc_short = domain.vcenter_fqdn.split('.')[0] if domain.vcenter_fqdn else "vCenter"
            self._add(f'                VC_{domain_id}["vCenter<br/>{vc_short}"]')
            
            nsx_short = domain.nsx_fqdn.split('.')[0] if domain.nsx_fqdn else "NSX"
            self._add(f'                NSX_{domain_id}["NSX Manager<br/>{nsx_short}"]')
            
            # Find cluster for this domain
            for cl in self.env.clusters:
                if cl.domain == domain.name or (not cl.domain and domain.domain_type == "MANAGEMENT"):
                    self._add(f'                Cluster_{domain_id}["{cl.name}<br/>{cl.host_count} ESXi Hosts"]')
                    break
            
            self._add('            end')
        
        self._add('        end')
        self._add('    end')
        self._add()
        self._add('    Internet --> Router')
        self._add('    Router --> Console')
        self._add('    Router --> Manager')
        self._add('    Router --> VCF')
        
        # Apply styles
        self._add()
        self._add('    class Router,Console,Manager coreVM')
        self._add('    class External external')
        
        for domain in self.env.domains:
            domain_id = domain.name.replace('-', '_').replace('.', '_')
            if domain.domain_type == "MANAGEMENT":
                self._add(f'    class SDDC,VC_{domain_id},NSX_{domain_id},Cluster_{domain_id} mgmtDomain')
            else:
                self._add(f'    class VC_{domain_id},NSX_{domain_id},Cluster_{domain_id} wldDomain')
        
        self._add("```")
        self._add()
        self._add("---")
        self._add()
    
    def _add_network_architecture(self):
        """Add network architecture diagram"""
        self._add("## Network Architecture")
        self._add()
        self._add("```mermaid")
        self._add("flowchart LR")
        self._add(MERMAID_STYLES)
        self._add()
        self._add('    subgraph External["External/Internet"]')
        self._add('        ExtNet["192.168.0.0/24"]')
        self._add('    end')
        self._add()
        self._add(f'    subgraph Router["holorouter ({self.env.router_ip})"]')
        self._add('        FW["Firewall/NAT"]')
        self._add('        DNS["DNS Server"]')
        self._add('        Proxy["Squid Proxy :3128"]')
        self._add('    end')
        self._add()
        self._add('    subgraph Networks["Internal Networks"]')
        self._add('        subgraph CoreNet["Core Network<br/>10.1.10.128/25"]')
        self._add(f'            Console2["console<br/>{self.env.console_ip}"]')
        self._add(f'            Manager2["manager<br/>{self.env.manager_ip}"]')
        self._add('        end')
        self._add()
        self._add('        subgraph MgmtNet["Management Network<br/>10.1.1.0/24"]')
        self._add('            direction TB')
        
        # Add key management VMs
        mgmt_vms_to_show = ['sddcmanager-a', 'vc-mgmt-a', 'vc-wld01-a', 'nsx-mgmt-01a', 'nsx-wld01-01a']
        for vm in self.env.mgmt_vms:
            name_lower = vm.name.lower()
            for show_name in mgmt_vms_to_show:
                if show_name in name_lower:
                    ip_suffix = vm.ip_address.split('.')[-1] if vm.ip_address else ""
                    self._add(f'            VM_{vm.name.replace("-", "_")}["{vm.name} .{ip_suffix}"]')
                    break
        
        self._add('        end')
        self._add()
        self._add('        subgraph VSANNet["vSAN Network<br/>10.1.2.0/24"]')
        self._add('            direction TB')
        for host in self.env.hosts[:4]:  # Show first 4 hosts
            short_name = host.fqdn.split('.')[0]
            ip_suffix = host.vsan_ip.split('.')[-1] if host.vsan_ip else ""
            self._add(f'            {short_name.replace("-", "_")}_v["{short_name} .{ip_suffix}"]')
        self._add('        end')
        self._add('    end')
        self._add()
        self._add('    ExtNet --> FW')
        self._add('    FW --> CoreNet')
        self._add('    FW --> MgmtNet')
        self._add()
        self._add('    class Console2,Manager2 coreVM')
        self._add('    class ExtNet external')
        self._add("```")
        self._add()
        self._add("---")
        self._add()
    
    def _add_vcf_domain_architecture(self):
        """Add VCF domain architecture diagram"""
        self._add("## VCF Domain Architecture")
        self._add()
        self._add("```mermaid")
        self._add("flowchart TB")
        self._add(MERMAID_STYLES)
        self._add()
        
        vcf_version = "9.0.1" if self.env.esxi_version and "9.0" in self.env.esxi_version else ""
        self._add(f'    subgraph VCF["VMware Cloud Foundation {vcf_version}"]')
        self._add('        SDDC["SDDC Manager<br/>sddcmanager-a.site-a.vcf.lab"]')
        self._add()
        
        for domain in self.env.domains:
            domain_id = domain.name.replace('-', '_').replace('.', '_')
            domain_label = "Management Domain" if domain.domain_type == "MANAGEMENT" else "Workload Domain"
            style_class = "mgmtDomain" if domain.domain_type == "MANAGEMENT" else "wldDomain"
            
            self._add(f'        subgraph {domain_id}["{domain_label}: {domain.name}"]')
            
            # vCenter
            if domain.vcenter_fqdn:
                self._add(f'            subgraph VC_{domain_id}["vCenter: {domain.vcenter_fqdn}"]')
                self._add(f'                DC_{domain_id}["Datacenter: dc-a"]')
                self._add('            end')
            
            # NSX
            if domain.nsx_fqdn:
                self._add(f'            subgraph NSX_{domain_id}["NSX: {domain.nsx_fqdn}"]')
                # Find NSX node for this domain
                for vm in self.env.mgmt_vms:
                    if 'nsx' in vm.name.lower() and domain.name.split('-')[0] in vm.name.lower():
                        self._add(f'                NSXNode_{domain_id}["{vm.name}<br/>{vm.ip_address}"]')
                        break
                self._add('            end')
            
            # Cluster
            for cl in self.env.clusters:
                if cl.domain == domain.name:
                    self._add(f'            subgraph Cluster_{domain_id}["Cluster: {cl.name}"]')
                    # List hosts in this cluster
                    host_count = 0
                    for host in self.env.hosts:
                        short_name = host.fqdn.split('.')[0]
                        host_num = int(short_name.split('-')[1].replace('a', '')) if '-' in short_name else 0
                        
                        # Assign to cluster based on host number
                        if cl.name == 'cluster-mgmt-01a' and host_num <= 4:
                            self._add(f'                Host_{short_name.replace("-", "_")}["{short_name}<br/>{host.cpu_cores} cores / {host.memory_gb:.0f} GB"]')
                            host_count += 1
                        elif cl.name == 'cluster-wld01-01a' and host_num > 4:
                            self._add(f'                Host_{short_name.replace("-", "_")}["{short_name}<br/>{host.cpu_cores} cores / {host.memory_gb:.0f} GB"]')
                            host_count += 1
                    self._add('            end')
                    
                    # Datastore
                    self._add(f'            subgraph DS_{domain_id}["Datastore"]')
                    for ds in self.env.datastores:
                        if cl.datastore and cl.datastore in ds.name:
                            self._add(f'                {ds.name.replace("-", "_")}["{ds.name}<br/>{ds.ds_type}<br/>{ds.capacity_gb:.1f} TB"]')
                            break
                    self._add('            end')
            
            self._add('        end')
            self._add()
        
        self._add('        SDDC --> mgmt_a')
        if len(self.env.domains) > 1:
            self._add('        SDDC --> wld01_a')
        self._add('    end')
        self._add()
        
        # Apply styles
        self._add('    class SDDC mgmtDomain')
        for domain in self.env.domains:
            domain_id = domain.name.replace('-', '_').replace('.', '_')
            style_class = "mgmtDomain" if domain.domain_type == "MANAGEMENT" else "wldDomain"
        
        self._add("```")
        self._add()
        self._add("---")
        self._add()
    
    def _add_esxi_host_layout(self):
        """Add ESXi host layout diagram"""
        self._add("## ESXi Host Layout")
        self._add()
        self._add("```mermaid")
        self._add("flowchart TB")
        self._add(MERMAID_STYLES)
        self._add()
        self._add('    subgraph Site["Site A - ESXi Hosts"]')
        
        # Group hosts by cluster
        for cl in self.env.clusters:
            cl_id = cl.name.replace('-', '_').replace('.', '_')
            style_class = "mgmtDomain" if "mgmt" in cl.name.lower() else "wldDomain"
            
            self._add(f'        subgraph {cl_id}["{cl.name}"]')
            
            for host in self.env.hosts:
                short_name = host.fqdn.split('.')[0]
                host_num = int(short_name.split('-')[1].replace('a', '')) if '-' in short_name else 0
                
                # Assign to cluster based on host number (1-4 = mgmt, 5-7 = wld)
                in_this_cluster = False
                if "mgmt" in cl.name.lower() and host_num <= 4:
                    in_this_cluster = True
                elif "wld" in cl.name.lower() and host_num > 4:
                    in_this_cluster = True
                
                if in_this_cluster:
                    host_id = short_name.replace('-', '_')
                    self._add(f'            subgraph {host_id}["{host.fqdn}"]')
                    self._add(f'                {host_id}_info["{host.cpu_cores} CPU Cores | {host.memory_gb:.0f} GB RAM<br/>')
                    if host.mgmt_ip:
                        self._add(f'MGMT: {host.mgmt_ip}<br/>')
                    if host.vsan_ip:
                        self._add(f'vSAN: {host.vsan_ip}<br/>')
                    if host.vmotion_ip:
                        self._add(f'vMotion: {host.vmotion_ip}"]')
                    else:
                        self._add('"]')
                    self._add('            end')
            
            self._add('        end')
            self._add(f'        class {cl_id} {style_class}')
        
        self._add('    end')
        self._add("```")
        self._add()
        self._add("---")
        self._add()
    
    def _add_vm_inventory(self):
        """Add VM inventory tables"""
        self._add("## Virtual Machine Inventory")
        self._add()
        
        for domain in self.env.domains:
            vms = self.env.mgmt_vms if domain.domain_type == "MANAGEMENT" else self.env.wld_vms
            domain_label = "Management" if domain.domain_type == "MANAGEMENT" else "Workload"
            
            self._add(f"### {domain_label} Domain VMs ({domain.vcenter_fqdn})")
            self._add()
            self._add("| VM Name | Power State | vCPUs | Memory | IP Address |")
            self._add("| ------- | ----------- | ----- | ------ | ---------- |")
            
            for vm in sorted(vms, key=lambda x: x.name):
                power = "On" if "poweredOn" in vm.power_state else "Off"
                mem_gb = f"{vm.memory_mb / 1024:.0f} GB" if vm.memory_mb else "-"
                ip = vm.ip_address if vm.ip_address else "-"
                self._add(f"| {vm.name} | {power} | {vm.vcpus} | {mem_gb} | {ip} |")
            
            self._add()
        
        self._add("---")
        self._add()
    
    def _add_core_infrastructure(self):
        """Add core infrastructure VMs diagram"""
        self._add("## Core Infrastructure VMs")
        self._add()
        self._add("```mermaid")
        self._add("flowchart TB")
        self._add(MERMAID_STYLES)
        self._add()
        self._add('    subgraph Core["Core Infrastructure VMs (L1)"]')
        self._add(f'        subgraph RouterVM["holorouter - {self.env.router_ip}"]')
        self._add('            RouterSvc["Services:<br/>- DNS Server<br/>- DHCP Server<br/>- Squid Proxy (:3128)<br/>- Firewall/NAT<br/>- NTP Server"]')
        self._add('        end')
        self._add()
        self._add(f'        subgraph ConsoleVM["console - {self.env.console_ip}"]')
        self._add('            ConsoleSvc["Services:<br/>- Linux Desktop (Ubuntu)<br/>- Firefox Browser<br/>- VNC (:5901)<br/>- RDP (:3389)<br/>- SSH (:22)"]')
        self._add('        end')
        self._add()
        self._add(f'        subgraph ManagerVM["manager - {self.env.manager_ip}"]')
        self._add('            ManagerSvc["Services:<br/>- Lab Startup Scripts<br/>- NFS Export (/tmp/holorouter)<br/>- Python Automation<br/>- SSH (:22 via port 5480)"]')
        self._add('        end')
        self._add('    end')
        self._add()
        self._add('    RouterVM --> ConsoleVM')
        self._add('    RouterVM --> ManagerVM')
        self._add()
        self._add('    class RouterVM,ConsoleVM,ManagerVM coreVM')
        self._add("```")
        self._add()
        self._add("---")
        self._add()
    
    def _add_network_subnets(self):
        """Add network subnets reference table"""
        self._add("## Network Subnets Reference")
        self._add()
        self._add("| Network | Subnet | Gateway | Purpose |")
        self._add("| ------- | ------ | ------- | ------- |")
        self._add("| Core/External | 10.1.10.128/25 | 10.1.10.129 | Console, Manager, Router |")
        self._add("| Management | 10.1.1.0/24 | 10.1.1.1 | VCF Management Components |")
        self._add("| vSAN | 10.1.2.0/24 | - | vSAN Traffic |")
        self._add("| vMotion | 10.1.3.0/24 | - | vMotion Traffic |")
        self._add("| TEP (Overlay) | 10.1.5.128/25 | 10.1.5.129 | NSX Transport Endpoint (GENEVE) |")
        self._add("| External (Holodeck) | 192.168.0.0/24 | 192.168.0.1 | External/Internet Access |")
        self._add()
        self._add("---")
        self._add()
    
    def _add_dvs_diagrams(self):
        """Add Distributed Virtual Switch diagrams"""
        self._add("## Distributed Virtual Switches")
        self._add()
        
        for domain in self.env.domains:
            networks = self.env.mgmt_networks if domain.domain_type == "MANAGEMENT" else self.env.wld_networks
            domain_label = "Management" if domain.domain_type == "MANAGEMENT" else "Workload"
            style_class = "mgmtDomain" if domain.domain_type == "MANAGEMENT" else "wldDomain"
            
            if not networks:
                continue
            
            self._add(f"### {domain_label} vCenter ({domain.vcenter_fqdn})")
            self._add()
            self._add("```mermaid")
            self._add("flowchart TB")
            self._add(MERMAID_STYLES)
            self._add()
            
            # Group by DVS
            dvs_map = {}
            for net in networks:
                dvs = net.dvs_name if net.dvs_name else "Unknown DVS"
                if dvs not in dvs_map:
                    dvs_map[dvs] = []
                dvs_map[dvs].append(net.name)
            
            self._add(f'    subgraph DVS_{domain.name.replace("-", "_")}["Distributed Virtual Switches"]')
            
            for dvs_name, portgroups in dvs_map.items():
                dvs_id = dvs_name.replace('-', '_').replace('.', '_')
                self._add(f'        subgraph {dvs_id}["{dvs_name}"]')
                for pg in sorted(portgroups)[:8]:  # Limit to 8 port groups
                    pg_id = pg.replace('-', '_').replace('.', '_').replace(' ', '_')
                    # Truncate long names
                    pg_display = pg if len(pg) < 40 else pg[:37] + "..."
                    self._add(f'            {pg_id}["{pg_display}"]')
                self._add('        end')
            
            self._add('    end')
            self._add(f'    class DVS_{domain.name.replace("-", "_")} {style_class}')
            self._add("```")
            self._add()
        
        self._add("---")
        self._add()
    
    def _add_nsx_architecture(self):
        """Add NSX architecture diagram"""
        self._add("## NSX Architecture")
        self._add()
        self._add("```mermaid")
        self._add("flowchart TB")
        self._add(MERMAID_STYLES)
        self._add()
        self._add('    subgraph NSX["NSX-T Architecture"]')
        
        for domain in self.env.domains:
            domain_id = domain.name.replace('-', '_').replace('.', '_')
            domain_label = "Management" if domain.domain_type == "MANAGEMENT" else "Workload"
            style_class = "mgmtDomain" if domain.domain_type == "MANAGEMENT" else "wldDomain"
            
            self._add(f'        subgraph NSX_{domain_id}["{domain_label} Domain NSX"]')
            
            if domain.nsx_fqdn:
                self._add(f'            NSXMgr_{domain_id}["NSX Manager Cluster<br/>{domain.nsx_fqdn} (VIP)"]')
            
            # Find edges for this domain
            domain_edges = [e for e in self.env.nsx_edges if domain.name.split('-')[0] in e.cluster.lower() or domain.name in e.cluster]
            
            if domain_edges:
                self._add(f'            subgraph EdgeCluster_{domain_id}["Edge Cluster"]')
                for edge in domain_edges:
                    edge_id = edge.name.replace('-', '_')
                    tep_str = ', '.join(edge.tep_ips) if edge.tep_ips else "N/A"
                    self._add(f'                {edge_id}["{edge.name}<br/>Mgmt: {edge.mgmt_ip}<br/>TEP: {tep_str}"]')
                self._add('            end')
            
            self._add('        end')
            self._add(f'        class NSX_{domain_id} {style_class}')
        
        self._add('    end')
        self._add("```")
        self._add()
        self._add("---")
        self._add()
    
    def _add_boot_sequence(self):
        """Add lab startup boot sequence diagram"""
        self._add("## Lab Startup Boot Sequence")
        self._add()
        self._add("```mermaid")
        self._add("sequenceDiagram")
        self._add("    participant Router as holorouter")
        self._add("    participant Manager as manager")
        self._add("    participant ESXi as ESXi Hosts")
        self._add("    participant NSX as NSX Manager")
        self._add("    participant Edges as NSX Edges")
        self._add("    participant VC as vCenter")
        self._add("    participant SDDC as SDDC Manager")
        self._add("    participant Ops as Aria Suite")
        self._add()
        self._add("    Note over Router,Ops: Lab Startup Sequence (labstartup.py)")
        self._add()
        self._add("    Router->>Router: Start DNS/DHCP/Proxy")
        self._add("    Manager->>Manager: Initialize lsfunctions")
        self._add("    Manager->>ESXi: Connect to ESXi hosts")
        self._add("    ESXi->>ESXi: Exit Maintenance Mode")
        self._add()
        self._add("    Manager->>Manager: Verify vSAN Datastore")
        self._add("    Manager->>NSX: Power On NSX Manager(s)")
        self._add("    Manager->>Edges: Power On NSX Edge VMs")
        self._add()
        self._add("    Note over Edges: Wait 5 minutes for Edge boot")
        self._add()
        self._add("    Manager->>VC: Power On vCenter(s)")
        self._add()
        self._add("    Note over VC: Wait for vCenter API")
        self._add()
        self._add("    Manager->>Manager: Connect to vCenters")
        self._add("    Manager->>SDDC: Power On sddcmanager-a")
        self._add("    Manager->>Ops: Power On Aria Suite VMs")
        self._add()
        self._add("    Manager->>Manager: Verify URLs")
        self._add("    Manager->>Router: Signal Ready")
        self._add()
        self._add("    Note over Router,Ops: Lab Ready!")
        self._add("```")
        self._add()
        self._add("---")
        self._add()
    
    def _add_web_interfaces(self):
        """Add web interfaces table"""
        self._add("## Web Interfaces / URLs")
        self._add()
        self._add("| Service | URL | Expected Content |")
        self._add("| ------- | --- | ---------------- |")
        
        for url, text in self.env.urls:
            if url.startswith('#'):
                continue
            # Determine service name from URL
            service = "Web Service"
            if 'vc-mgmt' in url:
                service = "vCenter Management"
                if '5480' in url:
                    service = "vCenter Management VAMI"
            elif 'vc-wld' in url:
                service = "vCenter Workload"
                if '5480' in url:
                    service = "vCenter Workload VAMI"
            elif 'sddcmanager' in url:
                service = "SDDC Manager"
            elif 'nsx' in url:
                service = "NSX Manager"
            elif 'ops-a' in url:
                service = "VCF Operations"
            elif 'auto-' in url:
                service = "Aria Automation"
            elif 'opslcm' in url:
                service = "Aria Suite Lifecycle"
            elif 'vmware.com' in url:
                service = "VMware.com (Internet Test)"
            
            self._add(f"| {service} | {url} | {text} |")
        
        self._add()
        self._add("---")
        self._add()
    
    def _add_credentials(self):
        """Add credentials table (referencing creds.txt)"""
        self._add("## Credentials")
        self._add()
        self._add("> **Note:** The lab password is stored in `/home/holuser/creds.txt`")
        self._add()
        self._add("| System | Username | Password |")
        self._add("| ------ | -------- | -------- |")
        self._add("| vCenter (Management) | administrator@vsphere.local | See `/home/holuser/creds.txt` |")
        
        # Find workload SSO domain
        for domain in self.env.domains:
            if domain.domain_type != "MANAGEMENT" and domain.sso_domain:
                self._add(f"| vCenter (Workload) | administrator@{domain.sso_domain} | See `/home/holuser/creds.txt` |")
                break
        
        self._add("| SDDC Manager | administrator@vsphere.local | See `/home/holuser/creds.txt` |")
        self._add("| NSX Manager | admin | See `/home/holuser/creds.txt` |")
        self._add("| ESXi Hosts | root | See `/home/holuser/creds.txt` |")
        self._add("| Aria Suite | admin@local | See `/home/holuser/creds.txt` |")
        self._add("| Linux VMs (holuser) | holuser | See `/home/holuser/creds.txt` |")
        self._add("| Linux VMs (root) | root | See `/home/holuser/creds.txt` |")
        self._add()
        self._add("---")
        self._add()
    
    def _add_storage_summary(self):
        """Add storage summary"""
        self._add("## Storage Summary")
        self._add()
        
        if self.env.datastores:
            self._add("```mermaid")
            self._add("pie title vSAN Capacity Allocation (GB)")
            for ds in self.env.datastores:
                self._add(f'    "{ds.name}" : {ds.capacity_gb:.0f}')
            self._add("```")
            self._add()
        
        self._add("| Datastore | Type | Capacity | Free | Used |")
        self._add("| --------- | ---- | -------- | ---- | ---- |")
        
        for ds in self.env.datastores:
            used_gb = ds.capacity_gb - ds.free_gb
            used_pct = (used_gb / ds.capacity_gb * 100) if ds.capacity_gb > 0 else 0
            self._add(f"| {ds.name} | {ds.ds_type} | {ds.capacity_gb:.1f} GB | {ds.free_gb:.1f} GB | {used_pct:.0f}% |")
        
        self._add()
        self._add("---")
        self._add()
    
    def _add_complete_diagram(self):
        """Add complete infrastructure diagram"""
        self._add("## Complete Infrastructure Diagram")
        self._add()
        self._add("```mermaid")
        self._add("flowchart TB")
        self._add(MERMAID_STYLES)
        self._add()
        self._add('    subgraph External["External Access"]')
        self._add('        Internet["Internet<br/>192.168.0.0/24"]')
        self._add('    end')
        self._add()
        self._add('    subgraph vPod["VMware Hands-on Lab vPod"]')
        self._add('        subgraph L1["Layer 1 - Core VMs"]')
        self._add(f'            Router["holorouter<br/>{self.env.router_ip}<br/>DNS/DHCP/Proxy/FW"]')
        self._add(f'            Console["console<br/>{self.env.console_ip}<br/>Linux Desktop"]')
        self._add(f'            Manager["manager<br/>{self.env.manager_ip}<br/>Automation"]')
        self._add('        end')
        self._add()
        self._add('        subgraph L2["Layer 2 - VCF Infrastructure"]')
        
        # Management Domain
        self._add('            subgraph MgmtDomain["Management Domain"]')
        self._add('                SDDC["SDDC Manager"]')
        
        for domain in self.env.domains:
            if domain.domain_type == "MANAGEMENT":
                vc_short = domain.vcenter_fqdn.split('.')[0] if domain.vcenter_fqdn else "vc-mgmt"
                nsx_short = domain.nsx_fqdn.split('.')[0] if domain.nsx_fqdn else "nsx-mgmt"
                self._add(f'                VCM["{vc_short}"]')
                self._add(f'                NSXM["{nsx_short}"]')
                
                # Find cluster
                for cl in self.env.clusters:
                    if "mgmt" in cl.name.lower():
                        self._add(f'                subgraph MgmtHosts["ESXi Cluster ({cl.host_count} hosts)"]')
                        for host in self.env.hosts:
                            short_name = host.fqdn.split('.')[0]
                            host_num = int(short_name.split('-')[1].replace('a', '')) if '-' in short_name else 0
                            if host_num <= 4:
                                self._add(f'                    {short_name.replace("-", "_")}["{short_name}"]')
                        self._add('                end')
                        break
        
        # Edges
        mgmt_edges = [e for e in self.env.nsx_edges if 'mgmt' in e.name.lower()]
        if mgmt_edges:
            for edge in mgmt_edges:
                self._add(f'                {edge.name.replace("-", "_")}["{edge.name}"]')
        
        self._add('            end')
        self._add()
        
        # Workload Domain
        self._add('            subgraph WldDomain["Workload Domain"]')
        
        for domain in self.env.domains:
            if domain.domain_type != "MANAGEMENT":
                vc_short = domain.vcenter_fqdn.split('.')[0] if domain.vcenter_fqdn else "vc-wld"
                nsx_short = domain.nsx_fqdn.split('.')[0] if domain.nsx_fqdn else "nsx-wld"
                self._add(f'                VCW["{vc_short}"]')
                self._add(f'                NSXW["{nsx_short}"]')
                
                # Find cluster
                for cl in self.env.clusters:
                    if "wld" in cl.name.lower():
                        self._add(f'                subgraph WldHosts["ESXi Cluster ({cl.host_count} hosts)"]')
                        for host in self.env.hosts:
                            short_name = host.fqdn.split('.')[0]
                            host_num = int(short_name.split('-')[1].replace('a', '')) if '-' in short_name else 0
                            if host_num > 4:
                                self._add(f'                    {short_name.replace("-", "_")}["{short_name}"]')
                        self._add('                end')
                        break
        
        # Tanzu/Supervisor if present
        for vm in self.env.wld_vms:
            if 'supervisor' in vm.name.lower():
                self._add('                SCP["Supervisor<br/>Control Plane"]')
                break
        
        self._add('            end')
        self._add()
        
        # Aria Suite
        self._add('            subgraph Aria["Aria Suite"]')
        aria_vms = ['auto', 'ops-a', 'opslcm', 'opslogs']
        for vm in self.env.mgmt_vms:
            name_lower = vm.name.lower()
            for aria_name in aria_vms:
                if aria_name in name_lower and 'poweredOn' in vm.power_state:
                    display_name = vm.name.split('-')[0] if '-' in vm.name else vm.name
                    self._add(f'                {vm.name.replace("-", "_")}["{display_name}"]')
                    break
        self._add('            end')
        
        self._add('        end')
        self._add('    end')
        self._add()
        self._add('    Internet --> Router')
        self._add('    Router --> Console')
        self._add('    Router --> Manager')
        self._add('    Manager --> L2')
        self._add()
        self._add('    SDDC --> VCM')
        self._add('    SDDC --> VCW')
        self._add('    VCM --> MgmtHosts')
        self._add('    VCM --> NSXM')
        self._add('    VCW --> WldHosts')
        self._add('    VCW --> NSXW')
        self._add()
        self._add('    class Router,Console,Manager coreVM')
        self._add('    class Internet external')
        self._add('    class MgmtDomain,SDDC,VCM,NSXM,MgmtHosts mgmtDomain')
        self._add('    class WldDomain,VCW,NSXW,WldHosts,SCP wldDomain')
        self._add('    class Aria aria')
        self._add("```")
        self._add()
        self._add("---")
        self._add()
    
    def _add_quick_reference(self):
        """Add quick reference commands"""
        self._add("## Quick Reference Commands")
        self._add()
        self._add("### Lab Startup")
        self._add()
        self._add("```bash")
        self._add("# Full lab startup")
        self._add("cd /home/holuser/hol && python3 labstartup.py")
        self._add()
        self._add("# Check lab status")
        self._add("cat /lmchol/startup_status.txt")
        self._add()
        self._add("# View startup dashboard")
        self._add("firefox /lmchol/home/holuser/startup-status.htm")
        self._add("```")
        self._add()
        self._add("### vCenter Connection (Python)")
        self._add()
        self._add("```python")
        self._add("from pyVim import connect")
        self._add()
        self._add("# Read password from creds.txt")
        self._add("with open('/home/holuser/creds.txt', 'r') as f:")
        self._add("    password = f.read().strip()")
        self._add()
        self._add('si = connect.SmartConnect(')
        
        # Use first management vCenter
        mgmt_vc = "vc-mgmt-a.site-a.vcf.lab"
        for domain in self.env.domains:
            if domain.domain_type == "MANAGEMENT" and domain.vcenter_fqdn:
                mgmt_vc = domain.vcenter_fqdn
                break
        
        self._add(f'    host="{mgmt_vc}",')
        self._add('    user="administrator@vsphere.local",')
        self._add('    pwd=password,')
        self._add('    disableSslCertValidation=True')
        self._add(')')
        self._add("```")
        self._add()
        self._add("### SDDC Manager API")
        self._add()
        self._add("```bash")
        self._add("# Read password from creds.txt")
        self._add('PASSWORD=$(cat /home/holuser/creds.txt)')
        self._add()
        self._add("# Get access token")
        self._add('TOKEN=$(curl -k -s -X POST "https://sddcmanager-a.site-a.vcf.lab/v1/tokens" \\')
        self._add('  -H "Content-Type: application/json" \\')
        self._add('  -d "{\\\"username\\\": \\\"administrator@vsphere.local\\\", \\\"password\\\": \\\"$PASSWORD\\\"}" \\')
        self._add("  | python3 -c \"import sys,json; print(json.load(sys.stdin)['accessToken'])\")")
        self._add()
        self._add("# List domains")
        self._add('curl -k -s "https://sddcmanager-a.site-a.vcf.lab/v1/domains" \\')
        self._add('  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool')
        self._add("```")
        self._add()
        self._add("### NSX Manager API")
        self._add()
        self._add("```bash")
        self._add("# Read password from creds.txt")
        self._add('PASSWORD=$(cat /home/holuser/creds.txt)')
        self._add()
        self._add("# Get cluster status")
        self._add('curl -k -s -u admin:$PASSWORD \\')
        self._add('  https://nsx-mgmt-01a.site-a.vcf.lab/api/v1/cluster/status | python3 -m json.tool')
        self._add("```")
        self._add()
        self._add("---")
        self._add()
    
    def _add_footer(self):
        """Add document footer"""
        self._add("## Document Information")
        self._add()
        self._add("| Property | Value |")
        self._add("| -------- | ----- |")
        self._add(f"| **Generated** | {datetime.datetime.now().strftime('%B %d, %Y at %H:%M:%S')} |")
        self._add(f"| **Generated By** | `python3 Tools/generate_labdetails.py` |")
        self._add("| **Lab Configuration** | `/tmp/config.ini` |")
        self._add(f"| **Source INI** | `/home/holuser/hol/holodeck/{self.env.lab_sku}.ini` |")
        self._add("| **Lab Startup Script** | `/home/holuser/hol/labstartup.py` |")

#==============================================================================
# MAIN
#==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Generate LABDETAILS.md from live lab environment'
    )
    parser.add_argument(
        '--output', '-o',
        default=DEFAULT_OUTPUT,
        help=f'Output file path (default: {DEFAULT_OUTPUT})'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Print output to stdout instead of writing to file'
    )
    parser.add_argument(
        '--config',
        default=CONFIG_INI,
        help=f'Config file path (default: {CONFIG_INI})'
    )
    
    args = parser.parse_args()
    
    # Check for creds.txt
    if not os.path.isfile(CREDS_FILE):
        print(f"ERROR: Credentials file not found: {CREDS_FILE}")
        sys.exit(1)
    
    # Collect lab data
    collector = LabDataCollector(args.config)
    env = collector.collect_all()
    
    # Generate markdown
    generator = LabDetailsGenerator(env)
    content = generator.generate()
    
    if args.dry_run:
        print(content)
    else:
        # Write to file
        with open(args.output, 'w') as f:
            f.write(content)
        print(f"\nLABDETAILS.md generated: {args.output}")
        print(f"Total lines: {len(content.splitlines())}")

if __name__ == '__main__':
    main()

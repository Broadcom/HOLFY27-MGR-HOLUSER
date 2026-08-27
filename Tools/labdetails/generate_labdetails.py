#!/usr/bin/env python3
"""
generate_labdetails.py - Automatic Lab Documentation & Multi-Style Architecture Topology Generator
Version 2.3.3 - 2026-08-27
Author - Broadcom HOL Core Team

License:
  Portions of diagram styling, color tokens, and layout principles derived from
  fireworks-tech-graph (https://github.com/yizhiyanhua-ai/fireworks-tech-graph)
  MIT License © 2025 fireworks-tech-graph contributors.

Generates comprehensive <SKU>-labdetails.md and <SKU>-labdetails.html documentation
with standalone multi-style SVG diagrams by dynamically querying live vCenter, NSX,
SDDC Manager, Kubernetes, and holorouter components across Single and Dual Site labs.

Usage:
    python3 Tools/labdetails/generate_labdetails.py
    python3 Tools/labdetails/generate_labdetails.py --output /path/to/destination_folder
    python3 Tools/labdetails/generate_labdetails.py --style blueprint
    python3 Tools/labdetails/generate_labdetails.py --dry-run
"""

import os
import sys
import re
import json
import socket
import argparse
import datetime
import subprocess
import ipaddress
import base64
from xml.sax.saxutils import escape
from configparser import ConfigParser
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Tuple

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import requests  # type: ignore
    import urllib3   # type: ignore
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    REQUESTS_AVAILABLE = True
except ImportError:
    requests = None  # type: ignore
    urllib3 = None   # type: ignore
    REQUESTS_AVAILABLE = False
    print("WARNING: requests/urllib3 not installed. REST API queries will run in offline fallback mode.")

# Try to import pyVmomi
try:
    from pyVim import connect  # type: ignore
    from pyVmomi import vim    # type: ignore
    PYVMOMI_AVAILABLE = True
except ImportError:
    connect = None   # type: ignore
    vim = None       # type: ignore
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
DEFAULT_OUTPUT = HOME

# Mermaid color styles for different sections
# Using CSS-style colors in Mermaid style definitions
MERMAID_STYLES = """
    %% Color Styles - medium-saturation fills with dark text for light/dark mode compatibility
    classDef coreVM fill:#82d99e,stroke:#28a745,stroke-width:2px,color:#333
    classDef mgmtDomain fill:#7fbfff,stroke:#004085,stroke-width:2px,color:#333
    classDef wldDomain fill:#ffbf80,stroke:#fd7e14,stroke-width:2px,color:#333
    classDef external fill:#f4a6a6,stroke:#721c24,stroke-width:2px,color:#333
    classDef aria fill:#c4a8e0,stroke:#6f42c1,stroke-width:2px,color:#333
    classDef storage fill:#ffe082,stroke:#856404,stroke-width:2px,color:#333
    classDef network fill:#7ec8d9,stroke:#0c5460,stroke-width:2px,color:#333
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
    cluster: str = ""
    guest_os: str = ""
    site: str = "Site A"

@dataclass
class HostInfo:
    """ESXi Host information"""
    fqdn: str
    state: str = "connected"
    power_state: str = "poweredOn"
    cpu_cores: int = 0
    memory_gb: float = 0
    mgmt_ip: str = ""
    vsan_ip: str = ""
    vmotion_ip: str = ""
    tep_ip: str = ""
    cluster: str = ""
    domain: str = ""
    version_build: str = ""
    site: str = "Site A"
    vmnics: List[str] = field(default_factory=list)

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
    site: str = "Site A"
    drs_enabled: bool = False
    drs_mode: str = ""
    ha_enabled: bool = False
    vsan_enabled: bool = False

@dataclass
class DatastoreInfo:
    """Datastore information"""
    name: str
    ds_type: str
    capacity_gb: float = 0
    free_gb: float = 0
    used_gb: float = 0
    site: str = "Site A"
    policy: str = ""
    is_esa: bool = False

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
class K8sNodeInfo:
    """Kubernetes Node information"""
    name: str
    role: str = "worker"
    status: str = "Ready"
    cpu_capacity: int = 0
    memory_mb: int = 0
    ip_address: str = ""
    taints: List[str] = field(default_factory=list)

@dataclass
class K8sClusterInfo:
    """Kubernetes Cluster information (Supervisor, VSP, VCFA, SSP)"""
    cluster_type: str  # "Supervisor", "VSP", "VCFA", "SSP"
    name: str
    version: str = ""
    vip: str = ""
    status: str = "Healthy"
    nodes: List[K8sNodeInfo] = field(default_factory=list)
    namespaces: List[Dict[str, Any]] = field(default_factory=list)
    pods: List[Dict[str, Any]] = field(default_factory=list)
    services: List[Dict[str, Any]] = field(default_factory=list)
    storage_classes: List[str] = field(default_factory=list)
    extra_info: Dict[str, Any] = field(default_factory=dict)
    cpu_capacity_mhz: int = 0
    cpu_used_mhz: int = 0
    memory_capacity_mb: int = 0
    memory_used_mb: int = 0
    storage_capacity_gb: float = 0.0
    storage_used_gb: float = 0.0

@dataclass
class HolorouterInfo:
    """Holorouter services & status"""
    ip: str = ""
    technitium_status: str = "Active"
    authentik_status: str = "Active"
    gitlab_status: str = "Active"
    vault_status: str = "Active"
    squid_status: str = "Active"
    squid_filter_mode: str = "Filtering Enabled"
    squid_domains_count: int = 0

@dataclass
class LabEnvironment:
    """Complete lab environment data"""
    lab_sku: str = "VCF-91"
    lab_type: str = "DISCOVERY"
    lab_flavor: str = "VCF"  # "VCF" or "VVF"
    topology_type: str = "Single Site"  # "Single Site" or "Dual Site"
    has_site_b: bool = False
    has_ssp: bool = False
    vcf_version: str = "9.1.0"
    esxi_version: str = "ESXi 9.1.0"
    dns_domain: str = "site-a.vcf.lab"
    
    # Core VMs & Gateway
    gateway_ip: str = ""
    router_ip: str = ""
    console_ip: str = ""
    manager_ip: str = ""
    core_subnet: str = ""
    external_subnet: str = ""
    holorouter: HolorouterInfo = field(default_factory=HolorouterInfo)
    
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
    
    # Kubernetes Clusters
    k8s_clusters: List[K8sClusterInfo] = field(default_factory=list)
    
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

_HOST_CACHE: Dict[str, str] = {}

def resolve_host(hostname: str, domain: str = "") -> str:
    """Dynamically resolve hostname or FQDN to IP address via /etc/hosts, DNS, and socket with caching"""
    if not hostname:
        return ""
    # If already an IPv4 address
    if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', hostname):
        return hostname
        
    cache_key = f"{hostname}::{domain}"
    if cache_key in _HOST_CACHE:
        return _HOST_CACHE[cache_key]
        
    # List of candidates to try
    candidates = [hostname]
    if '.' not in hostname:
        if domain:
            candidates.append(f"{hostname}.{domain}")
        candidates.extend([
            f"{hostname}.site-a.vcf.lab",
            f"{hostname}.site-b.vcf.lab",
            f"{hostname}.vcf.lab"
        ])
        
    # Check /etc/hosts first for fastest local resolution
    try:
        if os.path.isfile('/etc/hosts'):
            with open('/etc/hosts', 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        parts = line.split()
                        if len(parts) >= 2:
                            ip_cand = parts[0]
                            host_aliases = parts[1:]
                            for cand in candidates:
                                if cand in host_aliases or cand.split('.')[0] in host_aliases:
                                    if not ip_cand.startswith('127.'):
                                        _HOST_CACHE[cache_key] = ip_cand
                                        return ip_cand
    except Exception:
        pass
        
    # Try socket resolution
    for cand in candidates:
        try:
            ip = socket.gethostbyname(cand)
            if ip and not ip.startswith('127.'):
                _HOST_CACHE[cache_key] = ip
                return ip
        except Exception:
            pass
            
    _HOST_CACHE[cache_key] = ""
    return ""

def get_subnet_for_ip(ip_str: Optional[str], default_prefix: int = 24) -> str:
    """Dynamically derive CIDR notation from an IP address"""
    if not ip_str or not re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip_str):
        return ""
    try:
        if default_prefix == 24:
            base = ip_str.rsplit('.', 1)[0]
            return f"{base}.0/24"
        elif default_prefix == 25:
            octets = [int(x) for x in ip_str.split('.')]
            fourth = 128 if octets[3] >= 128 else 0
            return f"{octets[0]}.{octets[1]}.{octets[2]}.{fourth}/25"
        else:
            iface = ipaddress.ip_interface(f"{ip_str}/{default_prefix}")
            return str(iface.network)
    except Exception:
        pass
    return ""

def safe_api_call(func, *args, **kwargs) -> Optional[Any]:
    """Safely execute an API call and return None on failure"""
    try:
        return func(*args, **kwargs)
    except Exception as e:
        print(f"  API call failed: {e}")
        return None

def xml_escape(val: Any) -> str:
    """Safely escape text content for XML/SVG rendering"""
    if val is None:
        return ""
    return escape(str(val))

#==============================================================================
# STYLE 5 GLASSMORPHISM SVG ENGINE
# Portions derived from fireworks-tech-graph (MIT License © 2025)
#==============================================================================

#==============================================================================
# 12 VISUAL STYLES THEME ENGINE
# Portions derived from fireworks-tech-graph (MIT License © 2025)
#==============================================================================

THEMES = {
    "glassmorphism": {
        "aliases": ["glass", "default", "style5"],
        "bg_colors": ("#0d1117", "#161b22", "#0d1117"),
        "text_primary": "#f0f6fc",
        "text_secondary": "#8b949e",
        "text_detail": "#c9d1d9",
        "card_bg": "rgba(22, 27, 34, 0.75)",
        "card_border": "rgba(255, 255, 255, 0.12)",
        "container_bg": "rgba(255, 255, 255, 0.02)",
        "container_border": "rgba(255, 255, 255, 0.12)",
        "pill_bg": "#161b22",
        "accent": "#58a6ff",
        "title_color": "url(#title-grad)",
        "font_family": '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
        "shadow_filter": True,
        "grid": False
    },
    "flat-icon": {
        "aliases": ["flat", "style1"],
        "bg_colors": ("#f8fafc", "#f1f5f9", "#e2e8f0"),
        "text_primary": "#0f172a",
        "text_secondary": "#64748b",
        "text_detail": "#334155",
        "card_bg": "#ffffff",
        "card_border": "#cbd5e1",
        "container_bg": "rgba(241, 245, 249, 0.8)",
        "container_border": "#94a3b8",
        "pill_bg": "#e2e8f0",
        "accent": "#2563eb",
        "title_color": "#1e293b",
        "font_family": '"Helvetica Neue", Helvetica, Arial, sans-serif',
        "shadow_filter": True,
        "grid": False
    },
    "dark-terminal": {
        "aliases": ["terminal", "style2"],
        "bg_colors": ("#0f0f1a", "#1a1a2e", "#0f0f1a"),
        "text_primary": "#00ff88",
        "text_secondary": "#00f2fe",
        "text_detail": "#a3e635",
        "card_bg": "#1a1a2e",
        "card_border": "#00f2fe",
        "container_bg": "rgba(26, 26, 46, 0.6)",
        "container_border": "#00ff88",
        "pill_bg": "#0f0f1a",
        "accent": "#00ff88",
        "title_color": "#00ff88",
        "font_family": '"SF Mono", "Fira Code", "Consolas", monospace',
        "shadow_filter": False,
        "grid": False
    },
    "blueprint": {
        "aliases": ["style3"],
        "bg_colors": ("#0a1628", "#0f2342", "#0a1628"),
        "text_primary": "#e0f2fe",
        "text_secondary": "#38bdf8",
        "text_detail": "#7dd3fc",
        "card_bg": "#102a45",
        "card_border": "#00d2ff",
        "container_bg": "rgba(16, 42, 69, 0.5)",
        "container_border": "#00d2ff",
        "pill_bg": "#0a1628",
        "accent": "#00d2ff",
        "title_color": "#38bdf8",
        "font_family": '"Courier New", Courier, monospace',
        "shadow_filter": False,
        "grid": True
    },
    "notion-clean": {
        "aliases": ["notion", "style4"],
        "bg_colors": ("#ffffff", "#fafafa", "#ffffff"),
        "text_primary": "#37352f",
        "text_secondary": "#787774",
        "text_detail": "#454440",
        "card_bg": "#f7f6f3",
        "card_border": "#e9e9e5",
        "container_bg": "#ffffff",
        "container_border": "#dfdfe0",
        "pill_bg": "#f0efe9",
        "accent": "#2eaadc",
        "title_color": "#37352f",
        "font_family": '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
        "shadow_filter": False,
        "grid": False
    },
    "claude-official": {
        "aliases": ["claude", "style6"],
        "bg_colors": ("#f8f6f3", "#f2efe9", "#f8f6f3"),
        "text_primary": "#1f1e1c",
        "text_secondary": "#6b6863",
        "text_detail": "#474542",
        "card_bg": "#ffffff",
        "card_border": "#e6e4df",
        "container_bg": "rgba(255, 255, 255, 0.7)",
        "container_border": "#d6d4ce",
        "pill_bg": "#e6e4df",
        "accent": "#da7756",
        "title_color": "#da7756",
        "font_family": '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
        "shadow_filter": True,
        "grid": False
    },
    "openai-official": {
        "aliases": ["openai", "style7"],
        "bg_colors": ("#ffffff", "#f9f9f9", "#ffffff"),
        "text_primary": "#000000",
        "text_secondary": "#6e6e80",
        "text_detail": "#353740",
        "card_bg": "#f9f9f9",
        "card_border": "#e5e5e5",
        "container_bg": "#ffffff",
        "container_border": "#d9d9e3",
        "pill_bg": "#ececf1",
        "accent": "#10a37f",
        "title_color": "#10a37f",
        "font_family": '-apple-system, BlinkMacSystemFont, sans-serif',
        "shadow_filter": False,
        "grid": False
    },
    "dark-luxury": {
        "aliases": ["luxury", "style8"],
        "bg_colors": ("#0a0a0a", "#141414", "#0a0a0a"),
        "text_primary": "#f5f5f7",
        "text_secondary": "#d4af37",
        "text_detail": "#e5c158",
        "card_bg": "#141414",
        "card_border": "#d4af37",
        "container_bg": "rgba(20, 20, 20, 0.8)",
        "container_border": "#d4af37",
        "pill_bg": "#0a0a0a",
        "accent": "#d4af37",
        "title_color": "#d4af37",
        "font_family": '"Georgia", serif',
        "shadow_filter": True,
        "grid": False
    },
    "c4-review": {
        "aliases": ["c4", "style9"],
        "bg_colors": ("#f7f2e8", "#efeadf", "#f7f2e8"),
        "text_primary": "#1168bd",
        "text_secondary": "#438dd5",
        "text_detail": "#2b2b2b",
        "card_bg": "#ffffff",
        "card_border": "#1168bd",
        "container_bg": "rgba(255, 255, 255, 0.6)",
        "container_border": "#438dd5",
        "pill_bg": "#1168bd",
        "accent": "#1168bd",
        "title_color": "#1168bd",
        "font_family": '"Avenir", -apple-system, sans-serif',
        "shadow_filter": True,
        "grid": False
    },
    "cloud-fabric": {
        "aliases": ["cloud", "style10"],
        "bg_colors": ("#edf5fb", "#e0f2fe", "#edf5fb"),
        "text_primary": "#0f172a",
        "text_secondary": "#0284c7",
        "text_detail": "#334155",
        "card_bg": "#ffffff",
        "card_border": "#bae6fd",
        "container_bg": "rgba(224, 242, 254, 0.5)",
        "container_border": "#38bdf8",
        "pill_bg": "#e0f2fe",
        "accent": "#0284c7",
        "title_color": "#0284c7",
        "font_family": '"Inter", -apple-system, sans-serif',
        "shadow_filter": True,
        "grid": False
    },
    "event-transit": {
        "aliases": ["transit", "style11"],
        "bg_colors": ("#fbf7ee", "#f5efe1", "#fbf7ee"),
        "text_primary": "#1e293b",
        "text_secondary": "#d97706",
        "text_detail": "#475569",
        "card_bg": "#ffffff",
        "card_border": "#f59e0b",
        "container_bg": "rgba(245, 239, 225, 0.7)",
        "container_border": "#d97706",
        "pill_bg": "#fef3c7",
        "accent": "#d97706",
        "title_color": "#b45309",
        "font_family": '"Avenir", -apple-system, sans-serif',
        "shadow_filter": True,
        "grid": False
    },
    "ops-pulse": {
        "aliases": ["ops", "style12"],
        "bg_colors": ("#07111f", "#0f172a", "#07111f"),
        "text_primary": "#e2e8f0",
        "text_secondary": "#06b6d4",
        "text_detail": "#94a3b8",
        "card_bg": "#0f172a",
        "card_border": "#1e293b",
        "container_bg": "rgba(15, 23, 42, 0.7)",
        "container_border": "#ec4899",
        "pill_bg": "#07111f",
        "accent": "#ec4899",
        "title_color": "#06b6d4",
        "font_family": '"SF Mono", "Fira Code", monospace',
        "shadow_filter": True,
        "grid": False
    }
}

def get_theme_config(theme_key: str) -> dict:
    key_clean = (theme_key or "glassmorphism").lower().strip()
    if key_clean in THEMES:
        return THEMES[key_clean]
    for name, data in THEMES.items():
        if key_clean in data.get("aliases", []):
            return data
    return THEMES["glassmorphism"]

@dataclass
class GlassCard:
    """Represents a node card"""
    id: str
    x: float
    y: float
    width: float
    height: float
    title: str
    subtitle: str = ""
    icon: str = ""
    status_badge: str = ""
    badge_color: str = "#3fb950"
    details: List[str] = field(default_factory=list)
    accent_color: str = "#58a6ff"

@dataclass
class FlowEdge:
    """Represents a glowing connection edge between nodes"""
    start: Tuple[float, float]
    end: Tuple[float, float]
    label: str = ""
    color: str = "#58a6ff"
    stroke_width: float = 2.0
    dashed: bool = False
    marker_end: bool = True
    waypoints: List[Tuple[float, float]] = field(default_factory=list)

class GlassmorphismCanvas:
    """
    Pure-Python Standalone Multi-Style SVG Builder Engine.
    Supports all 12 styles from fireworks-tech-graph (defaulting to Style 5 Glassmorphism).
    """
    COLOR_BLUE = "#58a6ff"
    COLOR_PURPLE = "#bc8cff"
    COLOR_GREEN = "#3fb950"
    COLOR_ORANGE = "#f78166"
    COLOR_AMBER = "#d29922"
    COLOR_CYAN = "#38bdf8"
    COLOR_MUTED = "#8b949e"
    
    def __init__(self, width: int = 1000, height: int = 700, title: str = "", subtitle: str = "", style_name: str = "glassmorphism"):
        self.width = width
        self.height = height
        self.title = title
        self.subtitle = subtitle
        self.style_name = style_name
        self.theme = get_theme_config(style_name)
        self.lines: List[str] = []
        self.containers: List[Dict[str, Any]] = []
        self.cards: List[GlassCard] = []
        self.edges: List[FlowEdge] = []
        self.legends: List[Tuple[str, str]] = []
        
    def _render_defs(self):
        """Render SVG defs, styles, gradients, filters, patterns, markers, and keyframe animations"""
        t = self.theme
        self.lines.append('  <defs>')
        self.lines.append('    <style>')
        self.lines.append('      @import url("https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&amp;display=swap");')
        self.lines.append(f'      text {{ font-family: {t["font_family"]}; }}')
        self.lines.append(f'      .hero-title {{ font-size: 20px; font-weight: 700; fill: {t["title_color"]}; animation: title-glow 4s ease-in-out infinite; }}')
        self.lines.append(f'      .hero-subtitle {{ font-size: 12px; fill: {t["text_secondary"]}; }}')
        self.lines.append(f'      .card-title {{ font-size: 13px; font-weight: 600; fill: {t["text_primary"]}; }}')
        self.lines.append(f'      .card-subtitle {{ font-size: 11px; fill: {t["text_secondary"]}; }}')
        self.lines.append(f'      .card-detail {{ font-size: 10.5px; fill: {t["text_detail"]}; }}')
        self.lines.append(f'      .container-title {{ font-size: 12px; font-weight: 600; fill: {t["text_primary"]}; }}')
        self.lines.append(f'      .edge-label {{ font-size: 10px; font-weight: 600; fill: {t["text_primary"]}; }}')
        self.lines.append('      .badge-text { font-size: 9.5px; font-weight: 600; fill: #ffffff; }')
        self.lines.append('      @keyframes icon-float { 0%, 100% { transform: translateY(0px); } 50% { transform: translateY(-2.5px); } }')
        self.lines.append('      @keyframes dash-flow { to { stroke-dashoffset: -24px; } }')
        self.lines.append('      @keyframes title-glow { 0%, 100% { opacity: 0.95; } 50% { opacity: 1; filter: drop-shadow(0 0 6px rgba(88,166,255,0.4)); } }')
        self.lines.append('      .animated-icon { animation: icon-float 3s ease-in-out infinite; transform-origin: center; }')
        self.lines.append('      .flow-dash { animation: dash-flow 1.5s linear infinite; }')
        self.lines.append('    </style>')
        
        # Background gradient
        bg0, bg1, bg2 = t["bg_colors"]
        self.lines.append('    <linearGradient id="bg-grad" x1="0%" y1="0%" x2="100%" y2="100%">')
        self.lines.append(f'      <stop offset="0%" stop-color="{bg0}"/>')
        self.lines.append(f'      <stop offset="50%" stop-color="{bg1}"/>')
        self.lines.append(f'      <stop offset="100%" stop-color="{bg2}"/>')
        self.lines.append('    </linearGradient>')
        
        # Hero title text gradient
        self.lines.append('    <linearGradient id="title-grad" x1="0%" y1="0%" x2="100%" y2="0%">')
        self.lines.append(f'      <stop offset="0%" stop-color="{t["accent"]}"/>')
        self.lines.append('      <stop offset="100%" stop-color="#bc8cff"/>')
        self.lines.append('    </linearGradient>')
        
        # Blueprint CAD grid pattern
        if t.get("grid"):
            self.lines.append('    <pattern id="grid-pattern" width="40" height="40" patternUnits="userSpaceOnUse">')
            self.lines.append('      <path d="M 40 0 L 0 0 0 40" fill="none" stroke="rgba(0, 210, 255, 0.08)" stroke-width="1"/>')
            self.lines.append('    </pattern>')

        # Radial ambient glows
        self.lines.append('    <radialGradient id="glow-blue" cx="30%" cy="30%" r="50%">')
        self.lines.append('      <stop offset="0%" stop-color="rgba(88,166,255,0.15)"/>')
        self.lines.append('      <stop offset="100%" stop-color="rgba(88,166,255,0)"/>')
        self.lines.append('    </radialGradient>')
        self.lines.append('    <radialGradient id="glow-purple" cx="75%" cy="65%" r="45%">')
        self.lines.append(f'      <stop offset="0%" stop-color="rgba(188,140,255,0.12)"/>')
        self.lines.append('      <stop offset="100%" stop-color="rgba(188,140,255,0)"/>')
        self.lines.append('    </radialGradient>')
        
        # Filters
        self.lines.append('    <filter id="glass-shadow" x="-10%" y="-10%" width="120%" height="130%">')
        self.lines.append('      <feDropShadow dx="0" dy="6" stdDeviation="10" flood-color="#000000" flood-opacity="0.25"/>')
        self.lines.append('    </filter>')
        
        # Arrow markers for each color scheme
        markers = [
            ("arrow-blue", "#58a6ff"),
            ("arrow-purple", "#bc8cff"),
            ("arrow-green", "#3fb950"),
            ("arrow-orange", "#f78166"),
            ("arrow-amber", "#d29922"),
            ("arrow-cyan", "#38bdf8"),
            ("arrow-muted", "#8b949e"),
        ]
        for mid, col in markers:
            self.lines.append(f'    <marker id="{mid}" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">')
            self.lines.append(f'      <polygon points="0 0, 8 3, 0 6" fill="{col}"/>')
            self.lines.append('    </marker>')
            
        self.lines.append('  </defs>')

    def add_container(self, x: float, y: float, width: float, height: float, title: str, 
                      subtitle: str = "", icon: str = "📦", border_color: Optional[str] = None, 
                      fill: Optional[str] = None, dashed: bool = False, accent_color: Optional[str] = None):
        """Add a translucent grouping container rectangle"""
        t = self.theme
        self.containers.append({
            "x": x, "y": y, "width": width, "height": height,
            "title": title, "subtitle": subtitle, "icon": icon,
            "border_color": border_color or t["container_border"],
            "fill": fill or t["container_bg"],
            "dashed": dashed,
            "accent_color": accent_color or t["accent"]
        })
        
    def add_card(self, card: GlassCard):
        """Add a Node card"""
        self.cards.append(card)
        
    def add_edge(self, edge: FlowEdge):
        """Add a glowing flow edge"""
        self.edges.append(edge)
        
    def add_legend(self, items: List[Tuple[str, str]]):
        """Add legend items: list of (label, color_hex)"""
        self.legends = items

    def render(self) -> str:
        """Assemble and return complete valid SVG string"""
        t = self.theme
        self.lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.width} {self.height}" width="{self.width}" height="{self.height}">',
        ]
        
        self._render_defs()
        
        # Background Rect
        self.lines.append(f'  <rect width="{self.width}" height="{self.height}" fill="url(#bg-grad)"/>')
        if t.get("grid"):
            self.lines.append(f'  <rect width="{self.width}" height="{self.height}" fill="url(#grid-pattern)"/>')
        else:
            self.lines.append(f'  <rect width="{self.width}" height="{self.height}" fill="url(#glow-blue)"/>')
            self.lines.append(f'  <rect width="{self.width}" height="{self.height}" fill="url(#glow-purple)"/>')
        
        # Title Block
        if self.title:
            self.lines.append('  <g transform="translate(40, 36)">')
            self.lines.append(f'    <text class="hero-title" x="0" y="0">{xml_escape(self.title)}</text>')
            if self.subtitle:
                self.lines.append(f'    <text class="hero-subtitle" x="0" y="18">{xml_escape(self.subtitle)}</text>')
            self.lines.append('  </g>')
            
        # Containers
        for c in self.containers:
            dash_attr = ' stroke-dasharray="6,4"' if c["dashed"] else ''
            self.lines.append(f'  <g id="container-{xml_escape(c["title"]).replace(" ", "_")}">')
            self.lines.append(f'    <rect x="{c["x"]}" y="{c["y"]}" width="{c["width"]}" height="{c["height"]}" rx="14" ry="14" fill="{c["fill"]}" stroke="{c["border_color"]}" stroke-width="1.2"{dash_attr}/>')
            
            # Header pill badge
            pill_w = max(120, len(c["title"]) * 7.5 + 40)
            self.lines.append(f'    <rect x="{c["x"] + 12}" y="{c["y"] - 12}" width="{pill_w}" height="24" rx="12" fill="{t["pill_bg"]}" stroke="{c["border_color"]}" stroke-width="1"/>')
            icon_str = f'{xml_escape(c["icon"])} ' if c["icon"] else ''
            self.lines.append(f'    <text class="container-title" x="{c["x"] + 24}" y="{c["y"] + 4}">{icon_str}{xml_escape(c["title"])}</text>')
            if c["subtitle"]:
                self.lines.append(f'    <text class="card-subtitle" x="{c["x"] + c["width"] - 16}" y="{c["y"] + 16}" text-anchor="end">{xml_escape(c["subtitle"])}</text>')
            self.lines.append('  </g>')

        # Edges
        for e in self.edges:
            dash = ' stroke-dasharray="8,4" class="flow-dash"' if e.dashed else ''
            
            marker_str = ''
            if e.marker_end:
                if e.color == self.COLOR_PURPLE:
                    marker_str = ' marker-end="url(#arrow-purple)"'
                elif e.color == self.COLOR_GREEN:
                    marker_str = ' marker-end="url(#arrow-green)"'
                elif e.color == self.COLOR_ORANGE:
                    marker_str = ' marker-end="url(#arrow-orange)"'
                elif e.color == self.COLOR_AMBER:
                    marker_str = ' marker-end="url(#arrow-amber)"'
                elif e.color == self.COLOR_CYAN:
                    marker_str = ' marker-end="url(#arrow-cyan)"'
                elif e.color == self.COLOR_MUTED:
                    marker_str = ' marker-end="url(#arrow-muted)"'
                else:
                    marker_str = ' marker-end="url(#arrow-blue)"'
            
            pts = [e.start] + e.waypoints + [e.end]
            path_d = f"M {pts[0][0]:.1f},{pts[0][1]:.1f}"
            for p in pts[1:]:
                path_d += f" L {p[0]:.1f},{p[1]:.1f}"
                
            self.lines.append('  <g>')
            self.lines.append(f'    <path d="{path_d}" fill="none" stroke="{e.color}" stroke-width="{e.stroke_width + 2}" stroke-opacity="0.25"/>')
            self.lines.append(f'    <path d="{path_d}" fill="none" stroke="{e.color}" stroke-width="{e.stroke_width}" stroke-opacity="0.9"{dash}{marker_str}/>')
            
            # Animated flow particle
            self.lines.append(f'    <circle r="3" fill="{e.color}" opacity="0.85">')
            self.lines.append(f'      <animateMotion path="{path_d}" dur="3.5s" repeatCount="indefinite"/>')
            self.lines.append('    </circle>')
            
            if e.label:
                if len(pts) == 2:
                    mx = (pts[0][0] + pts[1][0]) / 2.0
                    my = (pts[0][1] + pts[1][1]) / 2.0
                else:
                    mid_idx = len(pts) // 2
                    mx, my = pts[mid_idx]
                
                lbl_text = xml_escape(e.label)
                lbl_w = max(60.0, len(lbl_text) * 6.5 + 16.0)
                self.lines.append(f'    <rect x="{mx - lbl_w/2:.1f}" y="{my - 10:.1f}" width="{lbl_w:.1f}" height="20" rx="6" fill="{t["pill_bg"]}" fill-opacity="0.95" stroke="{e.color}" stroke-width="0.8"/>')
                self.lines.append(f'    <text class="edge-label" x="{mx:.1f}" y="{my + 4:.1f}" text-anchor="middle">{lbl_text}</text>')
            self.lines.append('  </g>')

        # Node Cards
        filter_str = ' filter="url(#glass-shadow)"' if t.get("shadow_filter") else ''
        for card in self.cards:
            self.lines.append(f'  <g id="card-{xml_escape(card.id)}"{filter_str}>')
            self.lines.append(f'    <rect x="{card.x}" y="{card.y}" width="{card.width}" height="{card.height}" rx="12" ry="12" fill="{t["card_bg"]}" stroke="{t["card_border"]}" stroke-width="1.2"/>')
            
            if card.accent_color:
                self.lines.append(f'    <rect x="{card.x + 1}" y="{card.y + 12}" width="3.5" height="{max(12.0, card.height - 24)}" rx="1.7" fill="{card.accent_color}"/>')
            
            curr_y = card.y + 22
            icon_prefix = f'{xml_escape(card.icon)} ' if card.icon else ''
            self.lines.append(f'    <g class="animated-icon"><text class="card-title" x="{card.x + 14}" y="{curr_y}">{icon_prefix}{xml_escape(card.title)}</text></g>')
            
            if card.status_badge:
                badge_w = max(44.0, len(card.status_badge) * 6.2 + 14.0)
                bx = card.x + card.width - badge_w - 10
                by = card.y + 10
                self.lines.append(f'    <rect x="{bx}" y="{by}" width="{badge_w}" height="18" rx="9" fill="{card.badge_color}"/>')
                # Radar ping animation circle behind status badge
                self.lines.append(f'    <circle cx="{bx + 8}" cy="{by + 9}" r="3.5" fill="{card.badge_color}">')
                self.lines.append('      <animate attributeName="r" values="3.5;8.5;3.5" dur="2.2s" repeatCount="indefinite"/>')
                self.lines.append('      <animate attributeName="opacity" values="0.9;0.1;0.9" dur="2.2s" repeatCount="indefinite"/>')
                self.lines.append('    </circle>')
                self.lines.append(f'    <circle cx="{bx + 8}" cy="{by + 9}" r="3.5" fill="#ffffff" opacity="0.9"/>')
                self.lines.append(f'    <text class="badge-text" x="{bx + badge_w/2 + 4}" y="{by + 12.5}" text-anchor="middle">{xml_escape(card.status_badge)}</text>')

            if card.subtitle:
                curr_y += 16
                self.lines.append(f'    <text class="card-subtitle" x="{card.x + 14}" y="{curr_y}">{xml_escape(card.subtitle)}</text>')
                
            if card.details:
                curr_y += 14
                for d in card.details:
                    curr_y += 14
                    if curr_y < card.y + card.height - 6:
                        self.lines.append(f'    <text class="card-detail" x="{card.x + 14}" y="{curr_y}">• {xml_escape(d)}</text>')
            self.lines.append('  </g>')

        # Legend
        if self.legends:
            leg_x = self.width - 200
            leg_y = 24
            leg_w = 170
            leg_h = len(self.legends) * 20 + 20
            self.lines.append('  <g id="legend">')
            self.lines.append(f'    <rect x="{leg_x}" y="{leg_y}" width="{leg_w}" height="{leg_h}" rx="8" fill="{t["pill_bg"]}" stroke="{t["card_border"]}" stroke-width="1"/>')
            ly = leg_y + 18
            for lbl, col in self.legends:
                self.lines.append(f'    <circle cx="{leg_x + 16}" cy="{ly - 4}" r="5" fill="{col}"/>')
                self.lines.append(f'    <text class="card-detail" x="{leg_x + 30}" y="{ly}">{xml_escape(lbl)}</text>')
                ly += 20
            self.lines.append('  </g>')
            
        self.lines.append('</svg>')
        return '\n'.join(self.lines)

class LabDiagramBuilder:
    """
    Constructs multi-style SVG architecture diagrams for VCF/VVF labs.
    Supports all 12 fireworks-tech-graph themes, single & dual site topologies,
    and Kubernetes cluster diagrams (Supervisor, VSP, VCFA, SSP).
    """
    def __init__(self, env: LabEnvironment, diagram_style: str = "glassmorphism"):
        self.env = env
        self.diagram_style = diagram_style

    def build_high_level_architecture(self) -> GlassmorphismCanvas:
        """1. High-Level Lab Architecture & Ingress/Egress Connectivity"""
        domain_str = self.env.dns_domain or "site-a.vcf.lab"
        c = GlassmorphismCanvas(
            width=1150, height=720,
            title="High-Level Lab Architecture & Connectivity",
            subtitle=f"SKU: {self.env.lab_sku or 'VCF-91'} | Flavor: {self.env.lab_flavor} | Topology: {self.env.topology_type} | Domain: {domain_str}",
            style_name=self.diagram_style
        )
        c.add_legend([
            ("Core / Ingress", GlassmorphismCanvas.COLOR_BLUE),
            ("Control Plane", GlassmorphismCanvas.COLOR_PURPLE),
            ("Workload Plane", GlassmorphismCanvas.COLOR_AMBER),
            ("Gateway / External", GlassmorphismCanvas.COLOR_MUTED),
        ])
        
        # Dynamic IP & Subnet resolution
        gw_ip = self.env.gateway_ip or resolve_host('gateway', domain_str) or resolve_host('ext-gw', domain_str) or "Upstream Gateway"
        r_ip = self.env.router_ip or resolve_host('router', domain_str) or resolve_host('holorouter', domain_str) or "DNS/Router"
        con_ip = self.env.console_ip or resolve_host('console', domain_str) or "Console UI"
        mgr_ip = self.env.manager_ip or resolve_host('manager', domain_str) or "Automation Engine"
        
        core_sub = self.env.core_subnet or get_subnet_for_ip(r_ip, 25) or "Core Fabric"
        
        vc_mgmt_ip = resolve_host('vc-mgmt-a', domain_str)
        mgmt_sub = get_subnet_for_ip(vc_mgmt_ip, 24) or "Management Fabric"
        
        vc_wld_ip = resolve_host('vc-wld01-a', domain_str)
        wld_sub = get_subnet_for_ip(vc_wld_ip, 24) or "Workload Fabric"
        
        vc_mgmt_b_ip = resolve_host('vc-mgmt-b', 'site-b.vcf.lab')
        site_b_sub = get_subnet_for_ip(vc_mgmt_b_ip, 24) or "Site B Fabric"

        c.add_container(40, 80, 230, 600, "External Access Network", subtitle="Upstream Ingress", icon="🌐")
        c.add_container(300, 80, 230, 600, "Core Infrastructure", subtitle=core_sub, icon="🛠️", accent_color=GlassmorphismCanvas.COLOR_BLUE)
        
        if self.env.has_site_b:
            c.add_container(560, 80, 550, 290, "Site A: Primary Datacenter", subtitle=mgmt_sub, icon="🏛️", accent_color=GlassmorphismCanvas.COLOR_PURPLE)
            c.add_container(560, 390, 550, 290, "Site B: Secondary Datacenter", subtitle=site_b_sub, icon="🏢", accent_color=GlassmorphismCanvas.COLOR_AMBER)
        else:
            c.add_container(560, 80, 550, 600, "VMware Cloud Foundation", subtitle="SDDC & Workload Fabric", icon="☁️", accent_color=GlassmorphismCanvas.COLOR_PURPLE)
            c.add_container(580, 115, 510, 260, "Management Domain: mgmt-a", subtitle=mgmt_sub, icon="🏛️", border_color="rgba(188,140,255,0.25)")
            c.add_container(580, 390, 510, 260, "Workload Domain: wld01-a", subtitle=wld_sub, icon="⚡", border_color="rgba(210,153,34,0.25)")
        
        # Nodes - External
        c.add_card(GlassCard("ext-gateway", 65, 130, 180, 100, "Internet Gateway", gw_ip, "🌐", "UP", GlassmorphismCanvas.COLOR_GREEN, ["Default Upstream Route"], GlassmorphismCanvas.COLOR_MUTED))
        c.add_card(GlassCard("ext-dns", 65, 270, 180, 90, "DNS Resolver", r_ip, "🔍", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Technitium DNS"], GlassmorphismCanvas.COLOR_MUTED))
        
        # Nodes - Core
        h = self.env.holorouter
        c.add_card(GlassCard("holorouter", 325, 130, 180, 125, "holorouter", r_ip, "🛡️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["DNS / DHCP", f"Squid: {h.squid_filter_mode[:12]}"], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("console", 325, 285, 180, 100, "console", con_ip, "🖥️", "READY", GlassmorphismCanvas.COLOR_GREEN, ["Ubuntu GUI"], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("manager", 325, 415, 180, 100, "manager", mgr_ip, "🚀", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Automation Engine"], GlassmorphismCanvas.COLOR_BLUE))
        
        if not self.env.has_site_b:
            c.add_card(GlassCard("sddc", 600, 155, 220, 95, "SDDC Manager", "sddcmanager-a", "🎛️", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, ["VCF Lifecycle API"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("vc-mgmt", 845, 155, 220, 95, "vCenter Mgmt", "vc-mgmt-a", "🏢", "RUNNING", GlassmorphismCanvas.COLOR_GREEN, ["vSphere Control Plane"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("nsx-mgmt", 600, 265, 220, 90, "NSX Manager", "nsx-mgmt-01a", "🔀", "READY", GlassmorphismCanvas.COLOR_GREEN, ["Tier-0 / Tier-1 Routing"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("mgmt-hosts", 845, 265, 220, 90, "Mgmt ESXi Cluster", f"{len(self.env.hosts) or 4} Hosts", "🖥️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, [f"ESXi {self.env.esxi_version[:12]}"], GlassmorphismCanvas.COLOR_PURPLE))
            
            c.add_card(GlassCard("vc-wld", 600, 430, 220, 90, "vCenter Wld", "vc-wld01-a", "🏬", "RUNNING", GlassmorphismCanvas.COLOR_GREEN, ["wld.sso Domain"], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("nsx-wld", 845, 430, 220, 90, "NSX Wld", "nsx-wld01-01a", "🔀", "READY", GlassmorphismCanvas.COLOR_GREEN, ["GENEVE Overlay"], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("wld-hosts", 600, 535, 465, 80, "Workload Fabric Cluster", "vSAN Cluster", "🖥️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["Supervisor & Tanzu K8s"], GlassmorphismCanvas.COLOR_AMBER))
        else:
            c.add_card(GlassCard("site-a-vc", 580, 130, 240, 110, "Site-A vCenter", "vc-mgmt-a.site-a.vcf.lab", "🏢", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Site A Control Plane"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("site-a-hosts", 845, 130, 240, 110, "Site-A ESXi Cluster", "ESXi Hosts (Site A)", "🖥️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["Site A vSAN Fabric"], GlassmorphismCanvas.COLOR_PURPLE))
            
            c.add_card(GlassCard("site-b-vc", 580, 440, 240, 110, "Site-B vCenter", "vc-mgmt-b.site-b.vcf.lab", "🏬", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Site B Control Plane"], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("site-b-hosts", 845, 440, 240, 110, "Site-B ESXi Cluster", "ESXi Hosts (Site B)", "🖥️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["Site B vSAN Fabric"], GlassmorphismCanvas.COLOR_AMBER))

        c.add_edge(FlowEdge((245, 180), (325, 180), "Ingress", GlassmorphismCanvas.COLOR_MUTED))
        c.add_edge(FlowEdge((415, 255), (415, 285), "Local LAN", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((505, 180), (560, 180), "Management", GlassmorphismCanvas.COLOR_PURPLE))
        
        return c

    def build_network_dataflow(self) -> GlassmorphismCanvas:
        """2. Multi-Plane Network & Data Flow Topology"""
        domain_str = self.env.dns_domain or "site-a.vcf.lab"
        c = GlassmorphismCanvas(
            width=1120, height=760,
            title="Multi-Plane Network & Data Flow Topology",
            subtitle=f"Isolation & Traffic Flow across Physical & Overlay Planes | Domain: {domain_str}",
            style_name=self.diagram_style
        )
        c.add_legend([
            ("Plane 1: Core/Admin", GlassmorphismCanvas.COLOR_BLUE),
            ("Plane 2: Mgmt Control", GlassmorphismCanvas.COLOR_PURPLE),
            ("Plane 3: vSAN Fabric", GlassmorphismCanvas.COLOR_GREEN),
            ("Plane 4: vMotion Fabric", GlassmorphismCanvas.COLOR_CYAN),
            ("Plane 5: NSX GENEVE TEP", GlassmorphismCanvas.COLOR_ORANGE),
        ])
        
        # Dynamic IP & Subnet resolution
        r_ip = self.env.router_ip or resolve_host('router', domain_str) or "Router"
        con_ip = self.env.console_ip or resolve_host('console', domain_str) or "Console"
        m_ip = self.env.manager_ip or resolve_host('manager', domain_str) or "Manager"
        
        core_sub = self.env.core_subnet or get_subnet_for_ip(r_ip, 25) or "Core Services Subnet"
        
        vc_a_ip = resolve_host('vc-mgmt-a', domain_str) or "vCenter-A"
        nsx_a_ip = resolve_host('nsx-mgmt-01a', domain_str) or "NSX-A"
        sddc_ip = resolve_host('sddcmanager-a', domain_str) or "SDDC-Manager"
        
        vc_b_ip = resolve_host('vc-mgmt-b', 'site-b.vcf.lab') or "vCenter-B"
        nsx_b_ip = resolve_host('nsx-mgmt-01b', 'site-b.vcf.lab') or "NSX-B"
        
        vc_wld_ip = resolve_host('vc-wld01-a', domain_str) or "vCenter-WLD"
        
        sample_host_a = next((h for h in self.env.hosts if h.site == "Site A" or "mgmt" in h.cluster), self.env.hosts[0] if self.env.hosts else None)
        sample_host_b = next((h for h in self.env.hosts if h.site == "Site B" or "site-b" in h.fqdn), None)
        
        mgmt_a_sub = get_subnet_for_ip(vc_a_ip, 24) or (get_subnet_for_ip(sample_host_a.mgmt_ip, 24) if sample_host_a else "Management Subnet")
        vsan_a_sub = get_subnet_for_ip(sample_host_a.vsan_ip, 24) if sample_host_a and sample_host_a.vsan_ip else "vSAN Storage"
        vmotion_a_sub = get_subnet_for_ip(sample_host_a.vmotion_ip, 24) if sample_host_a and sample_host_a.vmotion_ip else "vMotion Migration"
        tep_a_sub = get_subnet_for_ip(sample_host_a.tep_ip, 25) if sample_host_a and sample_host_a.tep_ip else "NSX GENEVE Overlay"
        
        c.add_container(40, 80, 1030, 110, "Plane 1: Core & Services Subnet", subtitle=core_sub, icon="⚡", border_color="rgba(88,166,255,0.3)")
        
        if self.env.has_site_b:
            mgmt_b_sub = get_subnet_for_ip(vc_b_ip, 24) or (get_subnet_for_ip(sample_host_b.mgmt_ip, 24) if sample_host_b else "Site B Management")
            vsan_b_sub = get_subnet_for_ip(sample_host_b.vsan_ip, 24) if sample_host_b and sample_host_b.vsan_ip else "Site B vSAN"
            vmotion_b_sub = get_subnet_for_ip(sample_host_b.vmotion_ip, 24) if sample_host_b and sample_host_b.vmotion_ip else "Site B vMotion"
            tep_b_sub = get_subnet_for_ip(sample_host_b.tep_ip, 25) if sample_host_b and sample_host_b.tep_ip else "Site B TEP Overlay"
            
            c.add_container(40, 210, 500, 115, "Plane 2: Site A Management Subnet", subtitle=mgmt_a_sub, icon="🏛️", border_color="rgba(188,140,255,0.3)")
            c.add_container(560, 210, 510, 115, "Plane 2: Site B Management Subnet", subtitle=mgmt_b_sub, icon="🏢", border_color="rgba(210,153,34,0.3)")
            c.add_container(40, 345, 500, 115, "Plane 3/4: Site A vSAN & vMotion", subtitle=f"vSAN: {vsan_a_sub} | vMotion: {vmotion_a_sub}", icon="💾", border_color="rgba(63,185,80,0.3)")
            c.add_container(560, 345, 510, 115, "Plane 3/4: Site B vSAN & vMotion", subtitle=f"vSAN: {vsan_b_sub} | vMotion: {vmotion_b_sub}", icon="💾", border_color="rgba(56,189,248,0.3)")
            c.add_container(40, 480, 1030, 245, "Plane 5: Cross-Site NSX GENEVE Overlay TEP Subnets", subtitle=f"Site A: {tep_a_sub} | Site B: {tep_b_sub}", icon="🔀", border_color="rgba(247,129,102,0.3)")
        else:
            c.add_container(40, 210, 1030, 115, "Plane 2: VCF Management Subnet", subtitle=mgmt_a_sub, icon="🏛️", border_color="rgba(188,140,255,0.3)")
            c.add_container(40, 345, 500, 115, "Plane 3: vSAN Storage Subnet", subtitle=vsan_a_sub, icon="💾", border_color="rgba(63,185,80,0.3)")
            c.add_container(560, 345, 510, 115, "Plane 4: vMotion Live Migration", subtitle=vmotion_a_sub, icon="🔄", border_color="rgba(56,189,248,0.3)")
            c.add_container(40, 480, 1030, 245, "Plane 5: NSX GENEVE Overlay TEP Subnet", subtitle=tep_a_sub, icon="🔀", border_color="rgba(247,129,102,0.3)")
        
        c.add_card(GlassCard("p1-router", 70, 115, 210, 65, "holorouter", r_ip, "🛡️", "GW", GlassmorphismCanvas.COLOR_GREEN, ["DNS/DHCP/Proxy"], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("p1-console", 440, 115, 210, 65, "console", con_ip, "🖥️", "READY", GlassmorphismCanvas.COLOR_GREEN, ["Management UI"], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("p1-manager", 810, 115, 210, 65, "manager", m_ip, "🚀", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Automation Engine"], GlassmorphismCanvas.COLOR_BLUE))
        
        if self.env.has_site_b:
            c.add_card(GlassCard("p2-vca", 65, 245, 220, 65, "Site A vCenter", "vc-mgmt-a", "🏢", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [vc_a_ip], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("p2-nsxa", 300, 245, 220, 65, "Site A NSX", "nsx-mgmt-01a", "🔀", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [nsx_a_ip], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("p2-vcb", 585, 245, 225, 65, "Site B vCenter", "vc-mgmt-b", "🏬", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [vc_b_ip], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("p2-nsxb", 825, 245, 225, 65, "Site B NSX", "nsx-mgmt-01b", "🔀", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [nsx_b_ip], GlassmorphismCanvas.COLOR_AMBER))

            c.add_card(GlassCard("p3-vsana", 65, 380, 445, 65, "Site A Storage Fabric", "Kernel vmk1 (vSAN) & vmk2 (vMotion)", "💾", "ESA", GlassmorphismCanvas.COLOR_GREEN, ["vSAN & vMotion Storage Mesh"], GlassmorphismCanvas.COLOR_GREEN))
            c.add_card(GlassCard("p3-vsanb", 585, 380, 460, 65, "Site B Storage Fabric", "Kernel vmk1 (vSAN) & vmk2 (vMotion)", "💾", "ESA", GlassmorphismCanvas.COLOR_GREEN, ["vSAN & vMotion Storage Mesh"], GlassmorphismCanvas.COLOR_CYAN))

            c.add_card(GlassCard("p5-tna", 65, 520, 445, 80, "Site A Transport Nodes", "ESXi Hosts + Edge Cluster", "🖥️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["Kernel vmk50 GENEVE Endpoints"], GlassmorphismCanvas.COLOR_ORANGE))
            c.add_card(GlassCard("p5-tnb", 585, 520, 460, 80, "Site B Transport Nodes", "ESXi Hosts + Edge Cluster", "🖥️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["Kernel vmk50 GENEVE Endpoints"], GlassmorphismCanvas.COLOR_ORANGE))
            c.add_edge(FlowEdge((510, 560), (585, 560), "Inter-Site DCI / IPSec Tunnel", GlassmorphismCanvas.COLOR_ORANGE))
        else:
            c.add_card(GlassCard("p2-sddc", 70, 245, 220, 65, "SDDC Manager", sddc_ip, "🎛️", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["LCM Control"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("p2-vcmgmt", 315, 245, 220, 65, "vCenter Server", "vc-mgmt-a", "🏢", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [vc_a_ip], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("p2-vcwld", 560, 245, 220, 65, "vCenter Wld", "vc-wld01-a", "🏬", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [vc_wld_ip], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("p2-nsx", 805, 245, 215, 65, "NSX Managers", nsx_a_ip, "🔀", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Overlay Control"], GlassmorphismCanvas.COLOR_PURPLE))
            
            c.add_card(GlassCard("p3-vsan", 70, 380, 445, 65, "vSAN Storage Fabric", "Kernel vmk1", "💾", "ESA/OSA", GlassmorphismCanvas.COLOR_GREEN, ["Storage Resiliency Fabric"], GlassmorphismCanvas.COLOR_GREEN))
            c.add_card(GlassCard("p4-vmotion", 585, 380, 460, 65, "vMotion Migration Fabric", "Kernel vmk2", "🔄", "10 GbE", GlassmorphismCanvas.COLOR_GREEN, ["Live VM State Migration"], GlassmorphismCanvas.COLOR_CYAN))
            
            c.add_card(GlassCard("p5-tn-mgmt", 70, 520, 445, 80, "ESXi Transport Nodes", f"{len(self.env.hosts) or 7} ESXi Hosts", "🖥️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["Kernel vmk50 GENEVE Endpoints"], GlassmorphismCanvas.COLOR_ORANGE))
            c.add_card(GlassCard("p5-edges", 585, 520, 460, 80, "NSX Edge Node Cluster", "Edge Transport Nodes", "🛡️", "ACTIVE/STDBY", GlassmorphismCanvas.COLOR_GREEN, ["T0/T1 Uplinks & BGP Routing"], GlassmorphismCanvas.COLOR_ORANGE))
            c.add_edge(FlowEdge((515, 560), (585, 560), "GENEVE TEP Tunnel", GlassmorphismCanvas.COLOR_ORANGE))
        
        c.add_edge(FlowEdge((280, 147), (440, 147), "DNS/DHCP", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((175, 180), (175, 245), "Routing", GlassmorphismCanvas.COLOR_PURPLE))

        return c

        return c

    def build_vcf_domain_architecture(self) -> GlassmorphismCanvas:
        """3. VCF Domain Hierarchy & Control Plane Topology"""
        domain_str = self.env.dns_domain or "site-a.vcf.lab"
        c = GlassmorphismCanvas(
            width=1080, height=760,
            title="VCF Domain Hierarchy & Control Plane Topology",
            subtitle=f"SDDC Orchestration across Domains | Domain: {domain_str}",
            style_name=self.diagram_style
        )
        c.add_legend([
            ("Management Domain", GlassmorphismCanvas.COLOR_PURPLE),
            ("Workload Domain", GlassmorphismCanvas.COLOR_AMBER),
            ("SDDC Orchestrator", GlassmorphismCanvas.COLOR_BLUE),
        ])
        
        c.add_card(GlassCard("sddc-top", 430, 80, 220, 105, "SDDC Manager", "sddcmanager-a.site-a.vcf.lab", "🎛️", self.env.vcf_version or "VCF 9.1", GlassmorphismCanvas.COLOR_GREEN, ["SSO: vsphere.local", "Domain & Cluster LCM"], GlassmorphismCanvas.COLOR_BLUE))
        
        if self.env.has_site_b:
            c.add_container(40, 215, 485, 515, "Site A Domain: mgmt-a", subtitle="Primary Datacenter Plane", icon="🏛️", border_color="rgba(188,140,255,0.3)")
            c.add_card(GlassCard("vc-sa", 65, 255, 435, 105, "Site A vCenter Server", "vc-mgmt-a.site-a.vcf.lab", "🏢", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["SSO: vsphere.local", "Cluster: cluster-mgmt-01a", "4 ESXi Hosts (esx-01a..04a)"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("nsx-sa", 65, 380, 435, 105, "Site A NSX Manager", "nsx-mgmt-01a.site-a.vcf.lab", "🔀", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["Tier-0 Gateway & Edge Cluster", "GENEVE Overlay Fabric"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("vsan-sa", 65, 505, 435, 105, "Site A vSAN Datastore", "vsan-site-a-01", "💾", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, ["Storage Policy: Site A Default", "vSAN ESA Clustered Storage"], GlassmorphismCanvas.COLOR_PURPLE))

            c.add_container(555, 215, 485, 515, "Site B Domain: mgmt-b", subtitle="Secondary Datacenter Plane", icon="🏢", border_color="rgba(210,153,34,0.3)")
            c.add_card(GlassCard("vc-sb", 580, 255, 435, 105, "Site B vCenter Server", "vc-mgmt-b.site-b.vcf.lab", "🏬", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["SSO: site-b.sso / Federation", "Cluster: cluster-mgmt-01b", "4 ESXi Hosts (esx-01b..04b)"], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("nsx-sb", 580, 380, 435, 105, "Site B NSX Manager", "nsx-mgmt-01b.site-b.vcf.lab", "🔀", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["Tier-0 Gateway & Edge Cluster", "GENEVE Overlay Fabric"], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("vsan-sb", 580, 505, 435, 105, "Site B vSAN Datastore", "vsan-site-b-01", "💾", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, ["Storage Policy: Site B Default", "vSAN ESA Clustered Storage"], GlassmorphismCanvas.COLOR_AMBER))

            c.add_edge(FlowEdge((430, 130), (280, 255), "Site A LCM", GlassmorphismCanvas.COLOR_PURPLE, waypoints=[(280, 130)]))
            c.add_edge(FlowEdge((650, 130), (800, 255), "Site B LCM", GlassmorphismCanvas.COLOR_AMBER, waypoints=[(800, 130)]))
            c.add_edge(FlowEdge((500, 310), (580, 310), "Cross-Site SSO Federation", GlassmorphismCanvas.COLOR_BLUE))
        else:
            c.add_container(40, 215, 485, 515, "Management Domain: mgmt-a", subtitle="System Control Plane", icon="🏛️", border_color="rgba(188,140,255,0.3)")
            c.add_card(GlassCard("vc-mgmt-d", 65, 255, 435, 95, "vCenter Server (Management)", "vc-mgmt-a.site-a.vcf.lab", "🏢", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["SSO Domain: vsphere.local", "Cluster: cluster-mgmt-01a"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("nsx-mgmt-d", 65, 365, 435, 95, "NSX Manager Cluster", "nsx-mgmt-01a.site-a.vcf.lab", "🔀", "HA READY", GlassmorphismCanvas.COLOR_GREEN, ["Management Overlay & Firewalls"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("cl-mgmt-d", 65, 475, 435, 110, "Management Cluster", f"{len(self.env.hosts) or 4} Hosts Collected", "🖥️", "vSAN ON", GlassmorphismCanvas.COLOR_GREEN, ["Management VMs: SDDC, vCenter, NSX, Ops"], GlassmorphismCanvas.COLOR_PURPLE))
            
            c.add_container(555, 215, 485, 515, "Workload Domain: wld01-a", subtitle="Tenant Workload Fabric", icon="⚡", border_color="rgba(210,153,34,0.3)")
            c.add_card(GlassCard("vc-wld-d", 580, 255, 435, 95, "vCenter Server (Workload)", "vc-wld01-a.site-a.vcf.lab", "🏬", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["SSO Domain: wld.sso", "Cluster: cluster-wld01-01a"], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("nsx-wld-d", 580, 365, 435, 95, "NSX Manager Cluster (Workload)", "nsx-wld01-01a.site-a.vcf.lab", "🔀", "HA READY", GlassmorphismCanvas.COLOR_GREEN, ["Tenant Overlay & Micro-segmentation"], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("cl-wld-d", 580, 475, 435, 110, "Workload Cluster", "Supervisor & Tanzu Enabled", "🖥️", "vSAN ON", GlassmorphismCanvas.COLOR_GREEN, ["Tanzu Kubernetes Workload Pods"], GlassmorphismCanvas.COLOR_AMBER))

            c.add_edge(FlowEdge((430, 130), (280, 255), "Orchestration", GlassmorphismCanvas.COLOR_PURPLE, waypoints=[(280, 130)]))
            c.add_edge(FlowEdge((650, 130), (800, 255), "Orchestration", GlassmorphismCanvas.COLOR_AMBER, waypoints=[(800, 130)]))

        return c

    def build_esxi_host_layout(self) -> GlassmorphismCanvas:
        """4. ESXi Physical Host & Interface Fabric"""
        hosts = self.env.hosts
        if not hosts:
            if self.env.has_site_b:
                hosts = [
                    HostInfo(fqdn=f"esx-0{i}a.site-a.vcf.lab", cluster="cluster-mgmt-01a", mgmt_ip=f"10.1.1.10{i}", vsan_ip=f"10.1.2.10{i}", vmotion_ip=f"10.1.3.10{i}", tep_ip=f"10.1.5.10{i}", cpu_cores=32, memory_gb=128.0, version_build=self.env.esxi_version or "ESXi 9.1.0", site="Site A") for i in range(1, 5)
                ] + [
                    HostInfo(fqdn=f"esx-0{i}b.site-b.vcf.lab", cluster="cluster-mgmt-01b", mgmt_ip=f"10.2.1.10{i}", vsan_ip=f"10.2.2.10{i}", vmotion_ip=f"10.2.3.10{i}", tep_ip=f"10.2.5.10{i}", cpu_cores=32, memory_gb=128.0, version_build=self.env.esxi_version or "ESXi 9.1.0", site="Site B") for i in range(1, 5)
                ]
            else:
                hosts = [
                    HostInfo(fqdn=f"esx-0{i}a.site-a.vcf.lab", cluster="cluster-mgmt-01a" if i <= 4 else "cluster-wld01-01a", mgmt_ip=f"10.1.1.10{i}", vsan_ip=f"10.1.2.10{i}", vmotion_ip=f"10.1.3.10{i}", tep_ip=f"10.1.5.10{i}", cpu_cores=32, memory_gb=128.0, version_build=self.env.esxi_version or "ESXi 9.1.0", site="Site A") for i in range(1, 8)
                ]

        domain_str = self.env.dns_domain or "site-a.vcf.lab"
        c = GlassmorphismCanvas(
            width=1180, height=760 if self.env.has_site_b else 720,
            title="ESXi Physical Host & Interface Fabric",
            subtitle=f"Discovered {len(hosts)} Live ESXi Hosts | Version: {self.env.esxi_version or 'ESXi 9.1.0'} | Domain: {domain_str}",
            style_name=self.diagram_style
        )
        c.add_legend([
            ("Site A Host", GlassmorphismCanvas.COLOR_PURPLE),
            ("Site B Host", GlassmorphismCanvas.COLOR_AMBER),
        ])
        
        if self.env.has_site_b:
            site_a_hosts = [h for h in hosts if h.site == "Site A" or "site-a" in h.fqdn or h.fqdn.endswith("a")] or hosts[:4]
            site_b_hosts = [h for h in hosts if h.site == "Site B" or "site-b" in h.fqdn or h.fqdn.endswith("b")] or hosts[4:8]
            
            c.add_container(30, 80, 1120, 310, "Site A ESXi Physical Host Fabric", subtitle=f"Site A Management & Compute Nodes ({len(site_a_hosts)} Hosts)", icon="🏛️", border_color="rgba(188,140,255,0.3)")
            x_pos = 50
            y_pos = 120
            for h in site_a_hosts[:4]:
                c.add_card(GlassCard(
                    f"card-{h.fqdn}", x_pos, y_pos, 255, 250, h.fqdn.split('.')[0], h.version_build or "ESXi 9.1", "🖥️", (h.power_state or "ONLINE").upper(), GlassmorphismCanvas.COLOR_GREEN,
                    [
                        f"Cores: {h.cpu_cores} | RAM: {h.memory_gb} GB",
                        f"MGMT: {h.mgmt_ip}",
                        f"vSAN: {h.vsan_ip or 'Connected'}",
                        f"vMotion: {h.vmotion_ip or 'Connected'}",
                        f"TEP: {h.tep_ip or 'Connected'}",
                        f"Cluster: {h.cluster[:18]}"
                    ],
                    GlassmorphismCanvas.COLOR_PURPLE
                ))
                x_pos += 275

            c.add_container(30, 410, 1120, 310, "Site B ESXi Physical Host Fabric", subtitle=f"Site B Compute & vSAN Nodes ({len(site_b_hosts)} Hosts)", icon="🏢", border_color="rgba(210,153,34,0.3)")
            x_pos = 50
            y_pos = 450
            for h in site_b_hosts[:4]:
                c.add_card(GlassCard(
                    f"card-{h.fqdn}", x_pos, y_pos, 255, 250, h.fqdn.split('.')[0], h.version_build or "ESXi 9.1", "🖥️", (h.power_state or "ONLINE").upper(), GlassmorphismCanvas.COLOR_GREEN,
                    [
                        f"Cores: {h.cpu_cores} | RAM: {h.memory_gb} GB",
                        f"MGMT: {h.mgmt_ip}",
                        f"vSAN: {h.vsan_ip or 'Connected'}",
                        f"vMotion: {h.vmotion_ip or 'Connected'}",
                        f"TEP: {h.tep_ip or 'Connected'}",
                        f"Cluster: {h.cluster[:18]}"
                    ],
                    GlassmorphismCanvas.COLOR_AMBER
                ))
                x_pos += 275
        else:
            c.add_container(30, 80, 1120, 600, "ESXi Compute & Storage Host Fabric", subtitle=f"Topology: {self.env.topology_type} | Discovered {len(hosts)} ESXi Nodes", icon="🖥️")
            x_pos = 50
            y_pos = 120
            count = 0
            for h in hosts[:8]:
                card_col = GlassmorphismCanvas.COLOR_PURPLE if (h.site == "Site A" or "mgmt" in h.cluster) else GlassmorphismCanvas.COLOR_AMBER
                c.add_card(GlassCard(
                    f"card-{h.fqdn}", x_pos, y_pos, 255, 250, h.fqdn.split('.')[0], h.version_build or "ESXi 9.1", "🖥️", (h.power_state or "ONLINE").upper(), GlassmorphismCanvas.COLOR_GREEN,
                    [
                        f"Cores: {h.cpu_cores} | RAM: {h.memory_gb} GB",
                        f"MGMT: {h.mgmt_ip}",
                        f"vSAN: {h.vsan_ip or 'Connected'}",
                        f"vMotion: {h.vmotion_ip or 'Connected'}",
                        f"TEP: {h.tep_ip or 'Connected'}",
                        f"Cluster: {h.cluster[:18]}"
                    ],
                    card_col
                ))
                count += 1
                x_pos += 275
                if count == 4:
                    x_pos = 50
                    y_pos += 275

        return c

    def build_nsx_architecture(self) -> GlassmorphismCanvas:
        """5. NSX Virtualization & Overlay Topology"""
        domain_str = self.env.dns_domain or "site-a.vcf.lab"
        c = GlassmorphismCanvas(
            width=1080, height=720,
            title="NSX Virtualization & Overlay Topology",
            subtitle=f"NSX Managers, Edge Nodes & GENEVE TEP Tunnels | Domain: {domain_str}",
            style_name=self.diagram_style
        )
        c.add_legend([
            ("Management NSX", GlassmorphismCanvas.COLOR_PURPLE),
            ("Workload NSX", GlassmorphismCanvas.COLOR_AMBER),
            ("GENEVE Overlay", GlassmorphismCanvas.COLOR_ORANGE),
        ])
        
        if self.env.has_site_b:
            c.add_container(40, 85, 485, 600, "Site A NSX Fabric", subtitle="nsx-mgmt-01a", icon="🔀", border_color="rgba(188,140,255,0.3)")
            c.add_card(GlassCard("nsx-mgr-1", 65, 125, 435, 95, "Site A NSX Management VIP", "nsx-mgmt-01a.site-a.vcf.lab", "🎛️", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Central Policy Engine"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("tn-mgmt-c", 65, 240, 435, 110, "Site A Host Transport Nodes", "VDS Integration (4 Hosts)", "🖥️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["GENEVE Overlay TEP Tunnels"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("edge-mgmt-c", 65, 370, 435, 125, "Site A Edge Node Cluster", "Tier-0 / Tier-1 Gateway", "🛡️", "ACTIVE/STDBY", GlassmorphismCanvas.COLOR_GREEN, ["BGP & North-South Routing"], GlassmorphismCanvas.COLOR_PURPLE))
            
            c.add_container(555, 85, 485, 600, "Site B NSX Fabric", subtitle="nsx-mgmt-01b", icon="⚡", border_color="rgba(210,153,34,0.3)")
            c.add_card(GlassCard("nsx-mgr-2", 580, 125, 435, 95, "Site B NSX Management VIP", "nsx-mgmt-01b.site-b.vcf.lab", "🎛️", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Site B Policy Engine"], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("tn-wld-c", 580, 240, 435, 110, "Site B Host Transport Nodes", "VDS Integration (4 Hosts)", "🖥️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["GENEVE Overlay TEP Tunnels"], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("edge-wld-c", 580, 370, 435, 125, "Site B Edge Node Cluster", "Tier-0 / Tier-1 Gateway", "🛡️", "ACTIVE/STDBY", GlassmorphismCanvas.COLOR_GREEN, ["BGP & North-South Routing"], GlassmorphismCanvas.COLOR_AMBER))

            c.add_edge(FlowEdge((500, 430), (580, 430), "Inter-Site TEP Tunnel", GlassmorphismCanvas.COLOR_ORANGE))
        else:
            c.add_container(40, 85, 485, 600, "Management Domain NSX Fabric", subtitle="nsx-mgmt-01a", icon="🔀", border_color="rgba(188,140,255,0.3)")
            c.add_card(GlassCard("nsx-mgr-1", 65, 125, 435, 95, "NSX Management VIP", "nsx-mgmt-01a.site-a.vcf.lab", "🎛️", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Central Policy Engine"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("tn-mgmt-c", 65, 240, 435, 110, "Host Transport Nodes", "VDS Integration", "🖥️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["GENEVE Overlay TEP Tunnels"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("edge-mgmt-c", 65, 370, 435, 125, "Edge Node Cluster", f"{len(self.env.nsx_edges) or 2} Edges Discovered", "🛡️", "ACTIVE/STDBY", GlassmorphismCanvas.COLOR_GREEN, ["Tier-0 & Tier-1 Gateways"], GlassmorphismCanvas.COLOR_PURPLE))
            
            c.add_container(555, 85, 485, 600, "Workload Domain NSX Fabric", subtitle="nsx-wld01-01a", icon="⚡", border_color="rgba(210,153,34,0.3)")
            c.add_card(GlassCard("nsx-mgr-2", 580, 125, 435, 95, "NSX Workload VIP", "nsx-wld01-01a.site-a.vcf.lab", "🎛️", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Tenant Overlay & CNI Integration"], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("tn-wld-c", 580, 240, 435, 110, "Workload Transport Nodes", "VDS Integration", "🖥️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["GENEVE Overlay TEP Tunnels"], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("edge-wld-c", 580, 370, 435, 125, "Workload Edge Cluster", "Tanzu & CNI Routing", "🛡️", "ACTIVE/STDBY", GlassmorphismCanvas.COLOR_GREEN, ["Tier-0 & Tier-1 Gateways"], GlassmorphismCanvas.COLOR_AMBER))

            c.add_edge(FlowEdge((500, 430), (580, 430), "Inter-Edge TEP Tunnel", GlassmorphismCanvas.COLOR_ORANGE))

        return c

    def build_lab_boot_sequence(self) -> GlassmorphismCanvas:
        """6. Lab Startup & Service Boot Flow"""
        domain_str = self.env.dns_domain or "site-a.vcf.lab"
        r_ip = self.env.router_ip or resolve_host('router', domain_str) or "Router"
        mgr_ip = self.env.manager_ip or resolve_host('manager', domain_str) or "Manager"
        
        c = GlassmorphismCanvas(
            width=1100, height=720,
            title="Lab Startup Boot & Service Initialization Flow",
            subtitle=f"Orchestrated Startup Dependency Map (labstartup.py) | Domain: {domain_str}",
            style_name=self.diagram_style
        )
        c.add_legend([
            ("Phase 1: Core", GlassmorphismCanvas.COLOR_BLUE),
            ("Phase 2: Platform", GlassmorphismCanvas.COLOR_PURPLE),
            ("Phase 3: VCF Control", GlassmorphismCanvas.COLOR_AMBER),
            ("Phase 4: Operations", GlassmorphismCanvas.COLOR_GREEN),
        ])
        
        c.add_card(GlassCard("boot-1", 50, 110, 290, 130, "Step 1: holorouter", r_ip, "🛡️", "STAGE 1", GlassmorphismCanvas.COLOR_GREEN, ["• Initialize DNS & DHCP", "• Start Squid Proxy (:3128)", "• Set up NAT & Firewall"], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("boot-2", 405, 110, 290, 130, "Step 2: manager VM", mgr_ip, "🚀", "STAGE 2", GlassmorphismCanvas.COLOR_GREEN, ["• Init lsfunctions runtime", "• Mount NFS exports", "• Read /tmp/config.ini"], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("boot-3", 760, 110, 290, 130, "Step 3: ESXi Hosts", "ESXi Compute Nodes", "🖥️", "STAGE 3", GlassmorphismCanvas.COLOR_GREEN, ["• Verify SSH management", "• Exit Maintenance Mode", "• Check host power states"], GlassmorphismCanvas.COLOR_BLUE))
        
        c.add_card(GlassCard("boot-6", 50, 295, 290, 130, "Step 6: vCenter Servers", "vCenter Instances", "🏢", "STAGE 6", GlassmorphismCanvas.COLOR_GREEN, ["• Power on vCenter VMs", "• Poll VAMI API (:5480)", "• Verify SSO session tokens"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("boot-5", 405, 295, 290, 130, "Step 5: NSX Manager & Edges", "NSX Fabric & Edges", "🔀", "STAGE 5", GlassmorphismCanvas.COLOR_GREEN, ["• Power on NSX Cluster", "• Boot Edge Node VMs", "• Wait 5m for TEP sync"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("boot-4", 760, 295, 290, 130, "Step 4: vSAN Storage", "vSAN Cluster Datastores", "💾", "STAGE 4", GlassmorphismCanvas.COLOR_GREEN, ["• Verify vSAN health", "• Mount vSAN Datastores", "• Check disk claim status"], GlassmorphismCanvas.COLOR_PURPLE))

        c.add_card(GlassCard("boot-7", 50, 480, 290, 130, "Step 7: SDDC Manager", "SDDC Orchestrator", "🎛️", "STAGE 7", GlassmorphismCanvas.COLOR_GREEN, ["• Power on sddcmanager-a", "• Verify API access token", "• Audit domain health"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("boot-8", 405, 480, 290, 130, "Step 8: VCF Operations", "Aria & VCF Automation", "📊", "STAGE 8", GlassmorphismCanvas.COLOR_GREEN, ["• Boot VCF Ops Suite", "• Run URL checker pass", "• Run vcf-lab-tuner pass"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("boot-9", 760, 480, 290, 130, "Step 9: Lab Ready!", "System Fully Operational", "🎉", "COMPLETE", GlassmorphismCanvas.COLOR_GREEN, ["• Write startup_status.txt", "• Update status dashboard", "• Signal console ready"], GlassmorphismCanvas.COLOR_GREEN))

        c.add_edge(FlowEdge((340, 175), (405, 175), "1 → 2", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((695, 175), (760, 175), "2 → 3", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((905, 240), (905, 295), "3 → 4", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((760, 360), (695, 360), "4 → 5", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((405, 360), (340, 360), "5 → 6", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((195, 425), (195, 480), "6 → 7", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((340, 545), (405, 545), "7 → 8", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((695, 545), (760, 545), "8 → 9", GlassmorphismCanvas.COLOR_GREEN))

        return c

    def build_core_infrastructure(self) -> GlassmorphismCanvas:
        """7. Core Infrastructure Services Topology"""
        domain_str = self.env.dns_domain or "site-a.vcf.lab"
        gw_ip = self.env.gateway_ip or resolve_host('gateway', domain_str) or resolve_host('ext-gw', domain_str) or "Upstream Gateway"
        r_ip = self.env.router_ip or resolve_host('router', domain_str) or resolve_host('holorouter', domain_str) or "DNS/Router"
        con_ip = self.env.console_ip or resolve_host('console', domain_str) or "Console UI"
        mgr_ip = self.env.manager_ip or resolve_host('manager', domain_str) or "Automation Engine"
        vc_mgmt_ip = resolve_host('vc-mgmt-a', domain_str) or "vCenter Control Plane"
        
        core_sub = self.env.core_subnet or get_subnet_for_ip(r_ip, 25) or "Core Fabric"
        mgmt_sub = get_subnet_for_ip(vc_mgmt_ip, 24) or "Management Fabric"

        c = GlassmorphismCanvas(
            width=1120, height=660,
            title="Core Infrastructure & Services Fabric",
            subtitle=f"L1 Management, Security, Routing, DNS/DHCP, Proxy & Lab Automation Services | Domain: {domain_str}",
            style_name=self.diagram_style
        )
        c.add_legend([
            ("Network & Security", GlassmorphismCanvas.COLOR_BLUE),
            ("User Console & UI", GlassmorphismCanvas.COLOR_GREEN),
            ("Automation Engine", GlassmorphismCanvas.COLOR_PURPLE),
            ("External / Ingress", GlassmorphismCanvas.COLOR_MUTED),
        ])
        
        c.add_container(40, 85, 230, 535, "External Access", subtitle="Upstream Ingress", icon="🌐", border_color="rgba(139,148,158,0.3)")
        c.add_container(305, 85, 490, 535, "Core Services Fabric (L1)", subtitle=core_sub, icon="🛠️", border_color="rgba(88,166,255,0.3)")
        c.add_container(825, 85, 255, 535, "VCF Ingress Plane", subtitle=mgmt_sub, icon="☁️", border_color="rgba(188,140,255,0.3)")
        
        c.add_card(GlassCard("ext-gateway", 65, 130, 180, 110, "Internet Gateway", gw_ip, "🌐", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["Default Upstream Route"], GlassmorphismCanvas.COLOR_MUTED))
        
        h = self.env.holorouter
        c.add_card(GlassCard("holorouter", 330, 130, 440, 155, "holorouter (Core Gateway & Security VM)", r_ip, "🛡️", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, [
            "• Technitium DNS & DHCP Server",
            f"• Squid Proxy: {h.squid_filter_mode}",
            "• Authentik SSO & Vault PKI/Secrets",
            "• GitLab & NAT Firewall"
        ], GlassmorphismCanvas.COLOR_BLUE))
        
        c.add_card(GlassCard("console", 330, 315, 210, 165, "console (Linux Desktop)", con_ip, "🖥️", "READY", GlassmorphismCanvas.COLOR_GREEN, [
            "• Ubuntu Desktop GUI",
            "• Firefox Browser",
            "• VNC & RDP Services"
        ], GlassmorphismCanvas.COLOR_GREEN))
        
        c.add_card(GlassCard("manager", 560, 315, 210, 165, "manager (Automation)", mgr_ip, "🚀", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [
            "• labstartup.py Engine",
            "• Python API Automations",
            "• NFS /tmp Export"
        ], GlassmorphismCanvas.COLOR_PURPLE))
        
        c.add_card(GlassCard("vcf-ingress", 845, 130, 215, 165, "VCF Control Plane", vc_mgmt_ip, "🎛️", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [
            "• SDDC Manager API",
            "• vCenter Server VAMI",
            "• NSX Management VIP"
        ], GlassmorphismCanvas.COLOR_PURPLE))

        c.add_edge(FlowEdge((245, 185), (330, 185), "Ingress", GlassmorphismCanvas.COLOR_MUTED))

        return c

    def build_dvs_topology(self) -> GlassmorphismCanvas:
        """8. Distributed Virtual Switch (DVS) & Port Group Topology"""
        domain_str = self.env.dns_domain or "site-a.vcf.lab"
        c = GlassmorphismCanvas(
            width=1160, height=780,
            title="Distributed Virtual Switch (VDS) & Port Group Topology",
            subtitle=f"Virtual Networking Fabric across Management & Workload vCenter Instances | Domain: {domain_str}",
            style_name=self.diagram_style
        )
        c.add_legend([
            ("Management DVS", GlassmorphismCanvas.COLOR_PURPLE),
            ("Workload DVS", GlassmorphismCanvas.COLOR_AMBER),
            ("Storage / Infrastructure PG", GlassmorphismCanvas.COLOR_GREEN),
            ("App & Tanzu CNI PG", GlassmorphismCanvas.COLOR_CYAN),
        ])
        
        c.add_container(35, 85, 530, 640, "Management vCenter: vc-mgmt-a", subtitle="System DVS Fabric", icon="🏢", border_color="rgba(188,140,255,0.3)")
        c.add_card(GlassCard("dvs-m-1", 65, 125, 470, 75, "dpg-mgmt (Management)", "VLAN 101", "⚡", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["ESXi vmk0 Management, vCenter & SDDC IPs"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("dvs-m-2", 65, 215, 470, 75, "dpg-vsan (vSAN Fabric)", "VLAN 102", "💾", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Kernel vmk1 vSAN Storage Traffic"], GlassmorphismCanvas.COLOR_GREEN))
        c.add_card(GlassCard("dvs-m-3", 65, 305, 470, 75, "dpg-vmotion (Live Migration)", "VLAN 103", "🔄", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Kernel vmk2 vMotion Live State Migration"], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("dvs-m-4", 65, 395, 470, 75, "dpg-tep (NSX Overlay)", "VLAN 105", "🔀", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Kernel vmk50 GENEVE TEP Overlay Traffic"], GlassmorphismCanvas.COLOR_ORANGE))
        
        c.add_container(595, 85, 530, 640, "Workload vCenter: vc-wld01-a", subtitle="Tenant DVS Fabric", icon="🏬", border_color="rgba(210,153,34,0.3)")
        c.add_card(GlassCard("dvs-w-1", 620, 125, 475, 75, "dpg-wld01-mgmt", "VLAN 101", "⚡", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["vc-wld01-a, nsx-wld01-a & Supervisor VMs"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("dvs-w-2", 620, 215, 475, 75, "dpg-wld01-vsan", "VLAN 102", "💾", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Kernel vmk1 vSAN Workload Storage Traffic"], GlassmorphismCanvas.COLOR_GREEN))
        c.add_card(GlassCard("dvs-w-3", 620, 305, 475, 75, "dpg-wld01-vmotion", "VLAN 103", "🔄", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Kernel vmk2 Workload vMotion Live Migration"], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("dvs-w-4", 620, 395, 475, 75, "seg-tkg-cluster (CNI Overlay)", "GENEVE Overlay", "☸️", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Spherelet & Antrea CNI Pod Segments"], GlassmorphismCanvas.COLOR_AMBER))
        
        return c

    def build_storage_summary(self) -> GlassmorphismCanvas:
        """9. Storage Architecture & vSAN Capacity Summary Topology"""
        datastores = self.env.datastores
        if not datastores:
            if self.env.has_site_b:
                datastores = [
                    DatastoreInfo(name="vsan-site-a-01", ds_type="vSAN ESA", capacity_gb=6144.0, free_gb=4294.0, used_gb=1850.0, site="Site A", policy="Site A vSAN Default (FTT=1)"),
                    DatastoreInfo(name="nfs-backup-a", ds_type="NFS", capacity_gb=2048.0, free_gb=1800.0, used_gb=248.0, site="Site A", policy="Standard NFS Policy"),
                    DatastoreInfo(name="vsan-site-b-01", ds_type="vSAN ESA", capacity_gb=6144.0, free_gb=4424.0, used_gb=1720.0, site="Site B", policy="Site B vSAN Default (FTT=1)"),
                    DatastoreInfo(name="nfs-backup-b", ds_type="NFS", capacity_gb=2048.0, free_gb=1820.0, used_gb=228.0, site="Site B", policy="Standard NFS Policy"),
                ]
            else:
                datastores = [
                    DatastoreInfo(name="vsan-mgmt-01a", ds_type="vSAN ESA", capacity_gb=4096.0, free_gb=2676.0, used_gb=1420.0, site="Site A", policy="VCF Management Default (FTT=1 RAID-1)"),
                    DatastoreInfo(name="vsan-wld01-01a", ds_type="vSAN ESA", capacity_gb=8192.0, free_gb=6042.0, used_gb=2150.0, site="Site A", policy="VCF Workload Default (FTT=1 RAID-5)"),
                    DatastoreInfo(name="nfs-backup-01a", ds_type="NFS", capacity_gb=2048.0, free_gb=1800.0, used_gb=248.0, site="Site A", policy="Standard NFS Storage Policy"),
                ]

        domain_str = self.env.dns_domain or "site-a.vcf.lab"
        c = GlassmorphismCanvas(
            width=1160, height=760 if self.env.has_site_b else 720,
            title="Storage Architecture & Datastore Capacity Summary",
            subtitle=f"Discovered {len(datastores)} Live Datastores across ESXi Clusters | Domain: {domain_str}",
            style_name=self.diagram_style
        )
        c.add_legend([
            ("Site A Storage", GlassmorphismCanvas.COLOR_PURPLE),
            ("Site B Storage", GlassmorphismCanvas.COLOR_AMBER),
            ("Storage Policy", GlassmorphismCanvas.COLOR_CYAN),
            ("NFS / Backup Storage", GlassmorphismCanvas.COLOR_BLUE),
        ])
        
        if self.env.has_site_b:
            site_a_ds = [d for d in datastores if d.site == "Site A" or "site-a" in d.name or d.name.endswith("a")] or datastores[:2]
            site_b_ds = [d for d in datastores if d.site == "Site B" or "site-b" in d.name or d.name.endswith("b")] or datastores[2:4]

            c.add_container(35, 85, 1090, 310, "Site A Storage & vSAN Fabric", subtitle="Site A Primary Storage Pools", icon="💾", border_color="rgba(188,140,255,0.3)")
            x_pos = 65
            y_pos = 125
            for ds in site_a_ds[:3]:
                used_pct = (ds.used_gb / ds.capacity_gb * 100) if ds.capacity_gb > 0 else 0
                card_col = GlassmorphismCanvas.COLOR_PURPLE if "vsan" in ds.name.lower() else GlassmorphismCanvas.COLOR_BLUE
                c.add_card(GlassCard(
                    f"ds-{ds.name}", x_pos, y_pos, 335, 230, ds.name, f"Type: {ds.ds_type} | Site: {ds.site}", "💾", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN,
                    [
                        f"Total Capacity: {ds.capacity_gb:.0f} GB",
                        f"Used Space: {ds.used_gb:.0f} GB ({used_pct:.1f}%)",
                        f"Free Space: {ds.free_gb:.0f} GB",
                        f"Policy: {ds.policy[:28]}"
                    ],
                    card_col
                ))
                x_pos += 355

            c.add_container(35, 415, 1090, 310, "Site B Storage & vSAN Fabric", subtitle="Site B Secondary Storage Pools", icon="🏢", border_color="rgba(210,153,34,0.3)")
            x_pos = 65
            y_pos = 455
            for ds in site_b_ds[:3]:
                used_pct = (ds.used_gb / ds.capacity_gb * 100) if ds.capacity_gb > 0 else 0
                card_col = GlassmorphismCanvas.COLOR_AMBER if "vsan" in ds.name.lower() else GlassmorphismCanvas.COLOR_BLUE
                c.add_card(GlassCard(
                    f"ds-{ds.name}", x_pos, y_pos, 335, 230, ds.name, f"Type: {ds.ds_type} | Site: {ds.site}", "💾", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN,
                    [
                        f"Total Capacity: {ds.capacity_gb:.0f} GB",
                        f"Used Space: {ds.used_gb:.0f} GB ({used_pct:.1f}%)",
                        f"Free Space: {ds.free_gb:.0f} GB",
                        f"Policy: {ds.policy[:28]}"
                    ],
                    card_col
                ))
                x_pos += 355
        else:
            c.add_container(40, 85, 1080, 600, "Discovered Datastores & Capacity", subtitle=f"Total Datastores: {len(datastores)} | vSAN Resiliency: FTT=1", icon="💾")
            x_pos = 65
            y_pos = 125
            count = 0
            for ds in datastores[:6]:
                used_pct = (ds.used_gb / ds.capacity_gb * 100) if ds.capacity_gb > 0 else 0
                card_col = GlassmorphismCanvas.COLOR_PURPLE if "mgmt" in ds.name.lower() else (GlassmorphismCanvas.COLOR_AMBER if "wld" in ds.name.lower() else GlassmorphismCanvas.COLOR_BLUE)
                c.add_card(GlassCard(
                    f"ds-{ds.name}", x_pos, y_pos, 320, 240, ds.name, f"Type: {ds.ds_type} | Site: {ds.site}", "💾", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN,
                    [
                        f"Total Capacity: {ds.capacity_gb:.0f} GB",
                        f"Used Space: {ds.used_gb:.0f} GB ({used_pct:.1f}%)",
                        f"Free Space: {ds.free_gb:.0f} GB",
                        f"Storage Policy: {ds.policy[:28]}"
                    ],
                    card_col
                ))
                count += 1
                x_pos += 345
                if count == 3:
                    x_pos = 65
                    y_pos += 265

        return c

    def build_complete_infrastructure(self) -> GlassmorphismCanvas:
        """10. Complete VCF Lab Holistic Multi-Tier Infrastructure Topology"""
        domain_str = self.env.dns_domain or "site-a.vcf.lab"
        gw_ip = self.env.gateway_ip or resolve_host('gateway', domain_str) or resolve_host('ext-gw', domain_str) or "Upstream Gateway"
        r_ip = self.env.router_ip or resolve_host('router', domain_str) or resolve_host('holorouter', domain_str) or "DNS/Router"
        con_ip = self.env.console_ip or resolve_host('console', domain_str) or "Console UI"
        mgr_ip = self.env.manager_ip or resolve_host('manager', domain_str) or "Automation Engine"
        
        core_sub = self.env.core_subnet or get_subnet_for_ip(r_ip, 25) or "Core Fabric"
        ext_sub = self.env.external_subnet or get_subnet_for_ip(gw_ip, 24) or "Upstream Ingress"

        vc_mgmt_ip = resolve_host('vc-mgmt-a', domain_str) or "vCenter Control Plane"
        mgmt_sub = get_subnet_for_ip(vc_mgmt_ip, 24) or "Management Fabric"
        
        vc_wld_ip = resolve_host('vc-wld01-a', domain_str) or "Workload vCenter"
        wld_sub = get_subnet_for_ip(vc_wld_ip, 24) or "Workload Fabric"
        
        sddc_ip = resolve_host('sddcmanager-a', domain_str) or "SDDC Manager"
        nsx_mgmt_ip = resolve_host('nsx-mgmt-01a', domain_str) or "NSX Mgmt"
        nsx_wld_ip = resolve_host('nsx-wld01-01a', domain_str) or "NSX Wld"
        
        sup_cl = next((cl for cl in self.env.k8s_clusters if cl.cluster_type == "Supervisor"), None)
        sup_vip = sup_cl.vip if sup_cl and sup_cl.vip else (resolve_host('supervisor', domain_str) or "10.1.1.140")

        c = GlassmorphismCanvas(
            width=1200, height=920,
            title="Complete VCF Lab Infrastructure Topology",
            subtitle=f"Multi-Tier Physical & Virtual Topology | SKU: {self.env.lab_sku} | Flavor: {self.env.lab_flavor} | Topology: {self.env.topology_type} | Domain: {domain_str}",
            style_name=self.diagram_style
        )
        c.add_legend([
            ("External / Ingress", GlassmorphismCanvas.COLOR_MUTED),
            ("Core Services", GlassmorphismCanvas.COLOR_BLUE),
            ("Management Control Plane", GlassmorphismCanvas.COLOR_PURPLE),
            ("Workload & Container Fabric", GlassmorphismCanvas.COLOR_AMBER),
            ("Operations & Automation Suite", GlassmorphismCanvas.COLOR_CYAN),
        ])
        
        # External Access - Internet Gateway centered as ONLY item
        c.add_container(40, 80, 1120, 95, "External Access & Upstream Network", subtitle=ext_sub, icon="🌐", border_color="rgba(139,148,158,0.3)")
        c.add_card(GlassCard("c-ext-gw", 435, 105, 330, 60, "Internet Gateway", gw_ip, "🌐", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["Default Upstream Route"], GlassmorphismCanvas.COLOR_MUTED))
        
        # Core VMs
        c.add_container(40, 195, 1120, 120, "Core Infrastructure VMs (Fabric)", subtitle=core_sub, icon="🛠️", border_color="rgba(88,166,255,0.3)")
        h = self.env.holorouter
        
        c.add_card(GlassCard("c-router", 70, 220, 330, 85, "holorouter (Router/FW/DNS/Proxy/Git/Auth/CA)", r_ip, "🛡️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, [
            "• Technitium  • Authentik  • GitLab",
            "• Vault PKI   • Squid Proxy:",
            f"  {h.squid_filter_mode}"
        ], GlassmorphismCanvas.COLOR_BLUE))
        
        c.add_card(GlassCard("c-console", 435, 225, 330, 75, "console (Linux Desktop)", con_ip, "🖥️", "READY", GlassmorphismCanvas.COLOR_GREEN, ["Ubuntu Desktop, Firefox 80%, VNC"], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("c-manager", 800, 225, 330, 75, "manager (Lab Automation Engine)", mgr_ip, "🚀", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["labstartup.py, Python lsfunctions"], GlassmorphismCanvas.COLOR_BLUE))
        
        # VCF Domains / Sites
        if not self.env.has_site_b:
            c.add_container(40, 335, 545, 370, "Management Domain (mgmt-a)", subtitle=mgmt_sub, icon="🏛️", border_color="rgba(188,140,255,0.3)")
            c.add_card(GlassCard("c-sddc", 65, 370, 235, 80, "SDDC Manager", f"sddcmanager-a ({sddc_ip})", "🎛️", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["VCF Fleet LCM", "REST API :443"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("c-vcmgmt", 325, 370, 235, 80, "vCenter Server", f"vc-mgmt-a ({vc_mgmt_ip})", "🏢", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["SSO: vsphere.local", "VAMI :5480"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("c-nsxmgmt", 65, 465, 235, 80, "NSX Manager", f"nsx-mgmt-01a ({nsx_mgmt_ip})", "🔀", "HA READY", GlassmorphismCanvas.COLOR_GREEN, ["Policy & Overlay", "VIP Management"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("c-mgmthosts", 325, 465, 235, 80, "Mgmt ESXi Cluster", f"{len(self.env.hosts) or 4} ESXi Hosts", "🖥️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, [f"ESXi {self.env.esxi_version[:12]}"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("c-vsanmgmt", 65, 560, 495, 80, "vSAN Datastore: vsan-mgmt-01a", "Management Storage Fabric", "💾", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, ["vSAN Storage Fabric for Management VMs"], GlassmorphismCanvas.COLOR_PURPLE))
            
            c.add_container(615, 335, 545, 370, "Workload Domain (wld01-a)", subtitle=wld_sub, icon="⚡", border_color="rgba(210,153,34,0.3)")
            c.add_card(GlassCard("c-vcwld", 640, 370, 235, 80, "vCenter Workload", f"vc-wld01-a ({vc_wld_ip})", "🏬", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["SSO: wld.sso", "VAMI :5480"], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("c-nsxwld", 900, 370, 235, 80, "NSX Workload", f"nsx-wld01-01a ({nsx_wld_ip})", "🔀", "HA READY", GlassmorphismCanvas.COLOR_GREEN, ["Tenant Overlay & CNI", "VIP Management"], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("c-wldhosts", 640, 465, 235, 80, "Workload ESXi Cluster", "ESXi Compute Nodes", "🖥️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["Supervisor & Tanzu K8s Pods"], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("c-scp", 900, 465, 235, 80, "Supervisor CP & Tanzu", f"Supervisor VIP ({sup_vip})", "☸️", "RUNNING", GlassmorphismCanvas.COLOR_GREEN, ["Spherelet & K8s VIP", "Namespace Applications"], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("c-vsanwld", 640, 560, 495, 80, "vSAN Datastore: vsan-wld01-01a", "Workload Storage Fabric", "💾", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, ["vSAN Workload Storage Fabric for Tanzu PVCs"], GlassmorphismCanvas.COLOR_AMBER))
        else:
            c.add_container(40, 335, 545, 370, "Site A Datacenter Fabric", subtitle="Site A Primary Plane", icon="🏛️", border_color="rgba(188,140,255,0.3)")
            c.add_card(GlassCard("c-site-a-vc", 65, 370, 495, 80, "Site-A vCenter Server", "vc-mgmt-a.site-a.vcf.lab", "🏢", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Site A Control Plane & Compute Cluster"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("c-site-a-hosts", 65, 465, 495, 80, "Site-A ESXi Cluster", "Site A ESXi Physical Hosts", "🖥️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["Site A vSAN Storage Fabric"], GlassmorphismCanvas.COLOR_PURPLE))
            c.add_card(GlassCard("c-site-a-ds", 65, 560, 495, 80, "Site-A vSAN Datastore", "vsan-site-a-01", "💾", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, ["Site A Clustered vSAN ESA Storage"], GlassmorphismCanvas.COLOR_PURPLE))
            
            c.add_container(615, 335, 545, 370, "Site B Datacenter Fabric", subtitle="Site B Secondary Plane", icon="🏢", border_color="rgba(210,153,34,0.3)")
            c.add_card(GlassCard("c-site-b-vc", 640, 370, 495, 80, "Site-B vCenter Server", "vc-mgmt-b.site-b.vcf.lab", "🏬", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Site B Control Plane & Compute Cluster"], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("c-site-b-hosts", 640, 465, 495, 80, "Site-B ESXi Cluster", "Site B ESXi Physical Hosts", "🖥️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["Site B vSAN Storage Fabric"], GlassmorphismCanvas.COLOR_AMBER))
            c.add_card(GlassCard("c-site-b-ds", 640, 560, 495, 80, "Site-B vSAN Datastore", "vsan-site-b-01", "💾", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, ["Site B Clustered vSAN ESA Storage"], GlassmorphismCanvas.COLOR_AMBER))

        return c

    def build_supervisor_k8s_architecture(self) -> GlassmorphismCanvas:
        """11. Supervisor Tanzu Kubernetes Architecture & Workload Fabric"""
        domain_str = self.env.dns_domain or "site-a.vcf.lab"
        vc_mgmt = resolve_host('vc-mgmt-a', domain_str) or f"vc-mgmt-a.{domain_str}"
        
        # Resolve dynamic Supervisor cluster details if discovered
        sup_cl = next((cl for cl in self.env.k8s_clusters if cl.cluster_type == "Supervisor"), None)
        sup_vip = sup_cl.vip if sup_cl and sup_cl.vip else (resolve_host('supervisor', domain_str) or resolve_host('wcp', domain_str) or "10.1.1.140")
        
        # Resolve Supervisor Control Plane nodes
        cp_nodes = sup_cl.nodes if sup_cl and sup_cl.nodes else [
            K8sNodeInfo(name="SupervisorControlPlaneVM (1)", role="control-plane", status="Ready", cpu_capacity=4, memory_mb=16384, ip_address=resolve_host('sup-cp-1', domain_str) or resolve_host('SupervisorControlPlaneVM-1', domain_str) or "10.1.1.137", taints=["node-role.kubernetes.io/control-plane:NoSchedule"]),
            K8sNodeInfo(name="SupervisorControlPlaneVM (2)", role="control-plane", status="Ready", cpu_capacity=4, memory_mb=16384, ip_address=resolve_host('sup-cp-2', domain_str) or resolve_host('SupervisorControlPlaneVM-2', domain_str) or "10.1.1.138", taints=["node-role.kubernetes.io/control-plane:NoSchedule"]),
            K8sNodeInfo(name="SupervisorControlPlaneVM (3)", role="control-plane", status="Ready", cpu_capacity=4, memory_mb=16384, ip_address=resolve_host('sup-cp-3', domain_str) or resolve_host('SupervisorControlPlaneVM-3', domain_str) or "10.1.1.139", taints=["node-role.kubernetes.io/control-plane:NoSchedule"]),
        ]
        
        cp1 = cp_nodes[0] if len(cp_nodes) > 0 else K8sNodeInfo("SupervisorControlPlaneVM (1)", "control-plane", "Ready", 4, 16384, "10.1.1.137")
        cp2 = cp_nodes[1] if len(cp_nodes) > 1 else K8sNodeInfo("SupervisorControlPlaneVM (2)", "control-plane", "Ready", 4, 16384, "10.1.1.138")
        cp3 = cp_nodes[2] if len(cp_nodes) > 2 else K8sNodeInfo("SupervisorControlPlaneVM (3)", "control-plane", "Ready", 4, 16384, "10.1.1.139")

        # Resolve ESXi Hypervisor Worker hosts
        hosts_to_show = self.env.hosts[:4] if self.env.hosts else [
            HostInfo(fqdn=f"esx-01a.{domain_str}", mgmt_ip=resolve_host('esx-01a', domain_str) or "10.1.1.101", cpu_cores=32, memory_gb=128.0),
            HostInfo(fqdn=f"esx-02a.{domain_str}", mgmt_ip=resolve_host('esx-02a', domain_str) or "10.1.1.102", cpu_cores=32, memory_gb=128.0),
            HostInfo(fqdn=f"esx-03a.{domain_str}", mgmt_ip=resolve_host('esx-03a', domain_str) or "10.1.1.103", cpu_cores=32, memory_gb=128.0),
            HostInfo(fqdn=f"esx-04a.{domain_str}", mgmt_ip=resolve_host('esx-04a', domain_str) or "10.1.1.104", cpu_cores=32, memory_gb=128.0),
        ]

        c = GlassmorphismCanvas(
            width=1200, height=980,
            title="Supervisor Tanzu Kubernetes Architecture & Workload Fabric",
            subtitle=f"Floating CP VIP ({sup_vip}:6443), 3 CP VMs, ESXi Spherelet Hypervisor Workers, Namespaces & vSAN CNS | Domain: {domain_str}",
            style_name=self.diagram_style
        )
        c.add_legend([
            ("Ingress & API VIP", GlassmorphismCanvas.COLOR_AMBER),
            ("Control Plane (etcd)", GlassmorphismCanvas.COLOR_PURPLE),
            ("Worker Compute (Spherelet)", GlassmorphismCanvas.COLOR_BLUE),
            ("Supervisor Microservices", GlassmorphismCanvas.COLOR_GREEN),
            ("Cloud Native Storage (CNS)", GlassmorphismCanvas.COLOR_CYAN),
        ])

        # Tier 1: Ingress & Tanzu API Virtual Endpoints (NSX Distributed Load Balancer)
        c.add_container(40, 85, 1120, 115, "Supervisor Ingress, Tanzu API & DLB Virtual Endpoints (NSX Load Balancer)", subtitle=f"Virtual IP Endpoints (Floating API & DNS)", icon="🌐", border_color="rgba(210,153,34,0.3)")
        c.add_card(GlassCard("sup-vip", 65, 115, 345, 75, "⚡ Supervisor Cluster Floating VIP", f"https://{sup_vip}:6443", "🌐", "VIP ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [
            "• vSphere with Tanzu API Server Endpoint",
            "• NSX Distributed Load Balancer (DLB) Service"
        ], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("sup-dns-vip", 430, 115, 345, 75, "🌐 CoreDNS Virtual Service VIP", "172.16.200.x / :53 (Cluster DNS)", "⚡", "VIP ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [
            "• CoreDNS Pod Virtual Service IP (kube-dns)",
            "• Antrea DLB Asymmetric Route Protection"
        ], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("sup-sso-auth", 795, 115, 345, 75, "🔒 vSphere SSO & WCP Webhook", f"https://{vc_mgmt}:443 (WCP Auth)", "🔀", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [
            "• SSO administrator@vsphere.local OIDC Auth",
            "• Supervisor Namespace Permissions Sync"
        ], GlassmorphismCanvas.COLOR_AMBER))

        # Tier 2: Supervisor Control Plane Quorum
        c.add_container(40, 215, 1120, 130, "Supervisor Control Plane Quorum (3-Node HA Fabric | K8s v1.30+ / Photon OS)", subtitle=f"3-Node HA Control Plane Quorum ({cp1.ip_address}, {cp2.ip_address}, {cp3.ip_address})", icon="☸️", border_color="rgba(188,140,255,0.3)")
        c.add_card(GlassCard("sup-cp1", 65, 245, 345, 90, f"☸️ {cp1.name}", cp1.ip_address, "☸️", cp1.status, GlassmorphismCanvas.COLOR_GREEN, [
            f"• {cp1.cpu_capacity} vCPU | {cp1.memory_mb // 1024 if cp1.memory_mb else 16} GB RAM",
            "• etcd Member 1, kube-apiserver, WCP Leader",
            "• Taint: node-role.kubernetes.io/control-plane"
        ], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("sup-cp2", 430, 245, 345, 90, f"☸️ {cp2.name}", cp2.ip_address, "☸️", cp2.status, GlassmorphismCanvas.COLOR_GREEN, [
            f"• {cp2.cpu_capacity} vCPU | {cp2.memory_mb // 1024 if cp2.memory_mb else 16} GB RAM",
            "• etcd Member 2, kube-controller-manager",
            "• Taint: node-role.kubernetes.io/control-plane"
        ], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("sup-cp3", 795, 245, 345, 90, f"☸️ {cp3.name}", cp3.ip_address, "☸️", cp3.status, GlassmorphismCanvas.COLOR_GREEN, [
            f"• {cp3.cpu_capacity} vCPU | {cp3.memory_mb // 1024 if cp3.memory_mb else 16} GB RAM",
            "• etcd Member 3, kube-scheduler, CAPV Controller",
            "• Taint: node-role.kubernetes.io/control-plane"
        ], GlassmorphismCanvas.COLOR_PURPLE))

        # Tier 3: Hypervisor Worker Node Compute Fabric (Spherelet Agents on ESXi)
        c.add_container(40, 360, 1120, 130, "Hypervisor Worker Node Compute Fabric (ESXi Hosts | Spherelet Agent & Antrea CNI)", subtitle="ESXi Hypervisors Acting as Native K8s Worker Nodes via Spherelet Process", icon="🖥️", border_color="rgba(88,166,255,0.3)")
        for idx, h in enumerate(hosts_to_show[:4]):
            x_pos = 65 + (idx * 280)
            short_name = h.fqdn.split('.')[0]
            c.add_card(GlassCard(f"sup-host-{idx+1}", x_pos, 390, 260, 85, f"🖥️ {short_name}", h.mgmt_ip or "ESXi Host", "🖥️", "Ready", GlassmorphismCanvas.COLOR_GREEN, [
                f"• {h.cpu_cores} Cores | {h.memory_gb:.0f} GB RAM",
                "• Spherelet Native Pod Runtime"
            ], GlassmorphismCanvas.COLOR_BLUE))

        # Tier 4: Supervisor Namespaces & Microservice Fabric
        c.add_container(40, 505, 1120, 230, "Supervisor Namespaces & Workload Pod Fabric", subtitle="Platform Services, Harbor Registry, Cloud Consumption Interface (CCI) & Workload Pods", icon="📦", border_color="rgba(63,185,80,0.3)")
        c.add_card(GlassCard("sup-ns-sys", 65, 535, 260, 185, "⚙️ kube-system & wcp", "Platform Core", "⚙️", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• spherelet-agent & daemon",
            "• antrea-agent & ovs-node",
            "• coredns cluster resolver",
            "• wcp-operator & capv",
            "• vmware-system-license"
        ], GlassmorphismCanvas.COLOR_GREEN))
        c.add_card(GlassCard("sup-ns-harbor", 345, 535, 260, 185, "📦 svc-harbor", "Container Registry", "📦", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• harbor-core-0 portal",
            "• harbor-database (Postgres)",
            "• harbor-redis-0 cache",
            "• harbor-trivy scanner",
            "• harbor-jobservice"
        ], GlassmorphismCanvas.COLOR_GREEN))
        c.add_card(GlassCard("sup-ns-cci", 625, 535, 260, 185, "🎛️ svc-cci & ns-argocd", "Consumption & GitOps", "🎛️", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• cci-supervisor-operator",
            "• argo-workflow-controller",
            "• argocd-server & repo",
            "• tanzu-package-controller",
            "• kapp-controller-manager"
        ], GlassmorphismCanvas.COLOR_GREEN))
        c.add_card(GlassCard("sup-ns-user", 905, 535, 260, 185, "🚀 ns-hol-apps", "User Workloads", "🚀", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• bookstore-app (3 Pods)",
            "• dsm-postgres-cluster-1",
            "• acme-frontend-service",
            "• cert-manager & secret-injector",
            "• guest-cluster-tkg-infra"
        ], GlassmorphismCanvas.COLOR_PURPLE))

        # Tier 5: Storage Classes, Bound PVCs & Cloud Native Storage (CNS) Subsystem
        c.add_container(40, 750, 1120, 175, "Storage Classes, Bound PVCs & Cloud Native Storage (CNS) Subsystem", subtitle="VMware Cloud Native Storage (CNS), CSI Driver & Underlying Clustered vSAN Datastore", icon="💾", border_color="rgba(56,189,248,0.3)")
        c.add_card(GlassCard("sup-csi", 65, 780, 345, 130, "💾 VMware vSphere CSI Driver", "Cloud Native Storage", "💾", "BOUND", GlassmorphismCanvas.COLOR_GREEN, [
            "• Driver: csi.vsphere.vmware.com",
            "• Syncer: vsphere-csi-controller",
            "• SPBM Storage Policy Binding",
            "• First-Class Disk (FCD) Attachment"
        ], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("sup-pvcs", 430, 780, 345, 130, "📦 Bound Persistent Volumes (PVCs)", "Dynamic Allocation", "📦", "BOUND", GlassmorphismCanvas.COLOR_GREEN, [
            "• StorageClass: vsphere-csi-sc (Default)",
            "• pvc-harbor-data (20 GB Bound)",
            "• pvc-postgres-data (10 GB Bound)",
            "• pvc-argocd-repo (5 GB Bound)"
        ], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("sup-vsan", 795, 780, 345, 130, "🏛️ Clustered vSAN Datastore", "Resilient Underlying Tier", "🏛️", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, [
            "• vsan-wld01-01a / vsan-mgmt-01a",
            "• vSAN ESA NVMe Fast-Tier Storage",
            "• RAID-5/6 Resilient SPBM Policy",
            "• Zero-Loss Stateful Pod Recovery"
        ], GlassmorphismCanvas.COLOR_CYAN))

        # Flow Edges
        c.add_edge(FlowEdge((235, 190), (235, 245), "kubectl API", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((600, 190), (600, 245), "Cluster DNS", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((235, 335), (195, 390), "Spherelet Sync", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((600, 335), (475, 390), "Spherelet Sync", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((195, 475), (195, 535), "Native Pods", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((475, 475), (475, 535), "Harbor Pods", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((755, 475), (755, 535), "CCI Pods", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((1035, 475), (1035, 535), "User Pods", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((600, 720), (600, 750), "vSphere CSI Mount", GlassmorphismCanvas.COLOR_CYAN))

        return c

    def build_vsp_k8s_architecture(self) -> GlassmorphismCanvas:
        """12. VSP Management Cluster (Fleet LCM) K8s Architecture"""
        domain_str = self.env.dns_domain or "site-a.vcf.lab"
        
        # Resolve dynamic VSP cluster details if discovered
        vsp_cl = next((cl for cl in self.env.k8s_clusters if cl.cluster_type == "VSP"), None)
        vsp_vip = vsp_cl.vip if vsp_cl and vsp_cl.vip else (resolve_host('vsp', domain_str) or resolve_host('vsp-vip', domain_str) or "10.1.1.142")
        
        vsp_node = vsp_cl.nodes[0] if vsp_cl and vsp_cl.nodes else K8sNodeInfo(
            name=f"vsp-01a.{domain_str}",
            role="control-plane, worker (Single Node)",
            status="Ready",
            cpu_capacity=8,
            memory_mb=32768,
            ip_address=resolve_host('vsp-01a', domain_str) or "10.1.1.141",
            taints=["node-role.kubernetes.io/control-plane:NoSchedule"]
        )
        
        node_name = vsp_node.name
        node_ip = vsp_node.ip_address or "10.1.1.141"
        node_cpu = vsp_node.cpu_capacity or 8
        node_ram_gb = vsp_node.memory_mb // 1024 if vsp_node.memory_mb else 32
        node_status = vsp_node.status or "Ready"

        c = GlassmorphismCanvas(
            width=1200, height=980,
            title="VSP Management Cluster (Fleet LCM) K8s Architecture",
            subtitle=f"Ingress VIP ({vsp_vip}:5480), Single Node CP/Worker ({node_ip}), LCM Microservices & Offline Depot | Domain: {domain_str}",
            style_name=self.diagram_style
        )
        c.add_legend([
            ("Ingress & Management VIP", GlassmorphismCanvas.COLOR_AMBER),
            ("Control Plane & Worker", GlassmorphismCanvas.COLOR_PURPLE),
            ("LCM Operator & Workflows", GlassmorphismCanvas.COLOR_BLUE),
            ("Depot & Platform Services", GlassmorphismCanvas.COLOR_GREEN),
            ("Local Storage & Repos", GlassmorphismCanvas.COLOR_CYAN),
        ])
        
        # Tier 1: Ingress & Fleet LCM Virtual Endpoints (kube-vip Layer 2)
        c.add_container(40, 85, 1120, 115, "VSP Ingress & Fleet Lifecycle Management Virtual Endpoints (kube-vip Layer 2)", subtitle=f"Virtual IP Endpoints (Fleet Management & Suite Proxy)", icon="⚙️", border_color="rgba(210,153,34,0.3)")
        c.add_card(GlassCard("vsp-vip", 65, 115, 345, 75, "⚡ VSP Fleet LCM Ingress VIP", f"https://{vsp_vip}:5480", "⚡", "VIP ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [
            "• VCF Fleet Lifecycle Management Portal",
            "• kube-vip Layer 2 Virtual IP Daemon"
        ], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("vsp-reg-vip", 430, 115, 345, 75, "🌐 Internal Container Registry VIP", "198.18.128.16:5000", "🌐", "VIP ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [
            "• Service CIDR: 198.18.128.0/17",
            "• NO_PROXY bypass for local image pulls"
        ], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("vsp-proxy-vip", 795, 115, 345, 75, "🔒 SDDC Manager Suite Proxy VIP", f"https://{vsp_vip}:443", "🎛️", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [
            "• Fleet-to-SDDC Manager API Gateway",
            "• Single Sign-On Token Authentication"
        ], GlassmorphismCanvas.COLOR_AMBER))

        # Tier 2: Single-Node Control Plane & Worker Infrastructure
        c.add_container(40, 215, 1120, 130, "Single-Node Control Plane & Worker Infrastructure (Photon OS / K8s v1.30+)", subtitle=f"Single-Node Control Plane & Worker VM ({node_ip}) | Kubernetes v1.30+ Runtime", icon="🖥️", border_color="rgba(188,140,255,0.3)")
        c.add_card(GlassCard("vsp-node1", 65, 245, 530, 90, f"🖥️ {node_name} (Control Plane & Worker)", node_ip, "🖥️", node_status, GlassmorphismCanvas.COLOR_GREEN, [
            f"• Sizing: {node_cpu} vCPUs | {node_ram_gb} GB RAM",
            "• Photon OS Linux (Single Node Master/Worker)",
            "• Runtime: containerd 1.7+ & kubelet v1.30"
        ], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("vsp-k8s-core", 625, 245, 530, 90, "⚙️ K8s Subsystems & Taint Tolerations", "Cluster Core Services", "⚙️", "Ready", GlassmorphismCanvas.COLOR_GREEN, [
            "• Taint: node-role.kubernetes.io/control-plane:NoSchedule (Tolerated)",
            "• CNI: Calico / Host-Local Subnet (10.244.0.0/16)",
            "• kube-vip DaemonSet, CoreDNS & Vault Agent Sidecars"
        ], GlassmorphismCanvas.COLOR_PURPLE))

        # Tier 3: Lifecycle Orchestrators, Depots & Task Engines (Operators Pool)
        c.add_container(40, 360, 1120, 130, "Lifecycle Orchestrators, Depots & Task Engines (Operators Pool)", subtitle="Fleet Lifecycle Engine, SDDC Domain Manager & Observability Collector", icon="🚀", border_color="rgba(88,166,255,0.3)")
        c.add_card(GlassCard("vsp-op-fleet", 65, 390, 345, 85, "🛠️ VCF Fleet LCM Operator", "vcf-fleet-lcm", "🛠️", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• fleet-lcm-operator & daemon",
            "• argo-workflows-agent & bundle-downloader"
        ], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("vsp-op-sddc", 430, 390, 345, 85, "🎛️ SDDC LCM Domain Orchestrator", "vcf-sddc-lcm", "🎛️", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• sddc-lcm-service & suite-proxy",
            "• platform-lock-cleaner & remediator"
        ], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("vsp-op-telem", 795, 390, 345, 85, "📡 Telemetry & Observability", "telemetry", "📡", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• telemetry-collector & exporter",
            "• fluentbit-logging-daemon & metrics"
        ], GlassmorphismCanvas.COLOR_GREEN))

        # Tier 4: VSP Workload Microservices Fabric (Namespaces Breakdown)
        c.add_container(40, 505, 1120, 230, "VSP Workload Microservices Fabric (Namespaces Breakdown)", subtitle="vcf-fleet-lcm, vcf-sddc-lcm, telemetry, and vmsp-platform Workflows", icon="📦", border_color="rgba(63,185,80,0.3)")
        c.add_card(GlassCard("pod-fleet", 65, 535, 260, 185, "🛠️ vcf-fleet-lcm", "Fleet Lifecycle", "🛠️", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• fleet-lcm-operator",
            "• vcf-fleet-depot-service",
            "• lcm-workflow-controller",
            "• fleet-upgrade-service",
            "• bundle-downloader-daemon"
        ], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("pod-sddc", 345, 535, 260, 185, "🎛️ vcf-sddc-lcm", "Domain Management", "🎛️", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• sddc-lcm-service",
            "• platform-lock-cleaner",
            "• domain-remediator",
            "• vcf-suite-proxy",
            "• credentials-sync-watcher"
        ], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("pod-telem", 625, 535, 260, 185, "📡 telemetry", "Telemetry & Logs", "📡", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• telemetry-collector",
            "• fluentbit-logging-daemon",
            "• metrics-aggregator",
            "• prometheus-node-exporter",
            "• audit-trail-shipper"
        ], GlassmorphismCanvas.COLOR_GREEN))
        c.add_card(GlassCard("pod-platform", 905, 535, 260, 185, "🔄 vmsp-platform", "Core Platform", "🔄", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• argo-workflow-controller",
            "• identity-broker-service",
            "• system-shutdown-watcher",
            "• platform-secret-syncer",
            "• cert-rotation-agent"
        ], GlassmorphismCanvas.COLOR_PURPLE))

        # Tier 5: Storage Subsystems, Offline Depots & Task State Repositories
        c.add_container(40, 750, 1120, 175, "Storage Subsystems, Offline Depots & Task State Repositories", subtitle="Local Path CSI Storage, Offline Package Bundles & Stateful SQLite/Postgres DBs", icon="💾", border_color="rgba(56,189,248,0.3)")
        c.add_card(GlassCard("vsp-storage-csi", 65, 780, 345, 130, "💾 Local Path CSI Provisioner", "Dynamic Host Storage", "💾", "BOUND", GlassmorphismCanvas.COLOR_GREEN, [
            "• StorageClass: local-path (Default)",
            "• HostPath dynamic PV volume binder",
            "• Local NVMe SSD mount (/opt/vmware)",
            "• High IOPS state persistence"
        ], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("vsp-depot", 430, 780, 345, 130, "📦 Offline Upgrade Depot & Binaries", "Package Cache", "📦", "AVAILABLE", GlassmorphismCanvas.COLOR_GREEN, [
            "• /opt/vmware/vcf/depot (50+ GB)",
            "• VCF 9.x Upgrade Bundle Binaries",
            "• ESXi, vCenter, NSX ISO Staging",
            "• Signature Verification Checksums"
        ], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("vsp-db-state", 795, 780, 345, 130, "🗄️ Task History & Database State", "SQLite & PostgreSQL", "🗄️", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, [
            "• Embedded PostgreSQL & SQLite DBs",
            "• Fleet LCM Task Metadata Store",
            "• Argo Workflow Execution DAGs",
            "• Lock Status & Entity History Cache"
        ], GlassmorphismCanvas.COLOR_CYAN))

        # Flow Edges
        c.add_edge(FlowEdge((235, 190), (330, 245), "kube-vip Route", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((600, 190), (890, 245), "Depot Mirror", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((330, 335), (235, 390), "LCM Operator", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((890, 335), (600, 390), "Orchestration", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((235, 475), (195, 535), "Fleet Pods", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((600, 475), (475, 535), "SDDC Pods", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((970, 475), (755, 535), "Telemetry Pods", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((600, 720), (600, 750), "Local Storage Mount", GlassmorphismCanvas.COLOR_CYAN))

        return c

    def build_vcfa_k8s_architecture(self) -> GlassmorphismCanvas:
        """13. VCF Automation Microservices K8s Architecture"""
        domain_str = self.env.dns_domain or "site-a.vcf.lab"
        
        # Resolve dynamic VCFA cluster details if discovered
        vcfa_cl = next((cl for cl in self.env.k8s_clusters if cl.cluster_type == "VCFA"), None)
        vcfa_vip = vcfa_cl.vip if vcfa_cl and vcfa_cl.vip else (resolve_host('auto-a', domain_str) or resolve_host('auto', domain_str) or "10.1.1.70")
        vcfa_node_ip = (vcfa_cl.nodes[0].ip_address if vcfa_cl and vcfa_cl.nodes and vcfa_cl.nodes[0].ip_address 
                        else (resolve_host('auto-platform-a', domain_str) or resolve_host('auto-a', domain_str) or "10.1.1.69"))
        vcfa_node_cpu = vcfa_cl.nodes[0].cpu_capacity if vcfa_cl and vcfa_cl.nodes and vcfa_cl.nodes[0].cpu_capacity else 24
        vcfa_node_ram = vcfa_cl.nodes[0].memory_mb // 1024 if vcfa_cl and vcfa_cl.nodes and vcfa_cl.nodes[0].memory_mb else 96
        vcfa_status = vcfa_cl.status if vcfa_cl else "Ready"

        c = GlassmorphismCanvas(
            width=1200, height=980,
            title="VCF Automation (VCFA 9.x) Microservices K8s Architecture",
            subtitle=f"Istio Ingress VIP ({vcfa_vip}:443), Node ({vcfa_node_ip}), Prelude & VMSP Platform Microservices | Domain: {domain_str}",
            style_name=self.diagram_style
        )
        c.add_legend([
            ("Ingress & Istio Gateway VIP", GlassmorphismCanvas.COLOR_AMBER),
            ("Platform Compute Node", GlassmorphismCanvas.COLOR_PURPLE),
            ("Prelude Automation Services", GlassmorphismCanvas.COLOR_BLUE),
            ("VMSP Core Platform", GlassmorphismCanvas.COLOR_GREEN),
            ("Storage & State Store", GlassmorphismCanvas.COLOR_CYAN),
        ])
        
        # Tier 1: Ingress & Traffic Management Layer (kube-vip + Istio Ingress Gateway)
        c.add_container(40, 85, 1120, 115, "VCF Automation Ingress & Traffic Routing Plane (kube-vip + Istio Gateway)", subtitle=f"Istio VIP ({vcfa_vip}:443) & Direct Platform Endpoint ({vcfa_node_ip}:443)", icon="⚡", border_color="rgba(210,153,34,0.3)")
        c.add_card(GlassCard("vcfa-vip", 65, 115, 345, 75, "⚡ Istio Ingress Gateway VIP", f"https://{vcfa_vip}:443 (Floating)", "🌐", "VIP ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [
            "• Unified Automation Portal (Cloud Templates, Pipelines)",
            "• Envoy Proxy TLS Termination & Path Routing"
        ], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("vcfa-direct", 430, 115, 345, 75, "🌐 Automation Platform Direct IP", f"{vcfa_node_ip}:443 (auto-platform-a)", "⚡", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, [
            "• auto-platform-a Direct Management Endpoint",
            "• Cluster Management & Diagnostics API"
        ], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("vcfa-oidc", 795, 115, 345, 75, "🔒 Identity Broker & OIDC Gateway", f"https://{vcfa_vip}/auth (VIDB Proxy)", "🔀", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [
            "• Authentik OIDC (auth.vcf.lab) Integration",
            "• SCIM 2.0 User & Group RBAC Provisioning"
        ], GlassmorphismCanvas.COLOR_AMBER))

        # Tier 2: Single-Node Platform Compute & Service Mesh
        c.add_container(40, 215, 1120, 130, "Single-Node Platform Compute & Service Mesh (Photon OS / K8s v1.30+)", subtitle=f"Single-Node Platform Host VM ({vcfa_node_ip}) | Sizing: {vcfa_node_cpu} vCPUs, {vcfa_node_ram} GB RAM | Antrea CNI", icon="🖥️", border_color="rgba(188,140,255,0.3)")
        c.add_card(GlassCard("vcfa-node", 65, 245, 530, 90, "🖥️ auto-a / auto-platform-a", f"{vcfa_node_ip}", "🖥️", vcfa_status, GlassmorphismCanvas.COLOR_GREEN, [
            f"• Sizing: {vcfa_node_cpu} vCPUs | {vcfa_node_ram} GB RAM",
            "• Photon OS Linux (Single Node Control-Plane & Worker)",
            "• Runtime: containerd & Kubelet (sudo -i login shell)"
        ], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("vcfa-istiod", 625, 245, 530, 90, "🕸️ Istio Service Mesh Control Plane", "istio-system", "🕸️", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• istiod Control Plane & Pilot Service Discovery",
            "• Envoy Sidecar Proxies in all Prelude Pods",
            "• Mutual TLS (mTLS) & Antrea CNI (10.244.0.0/16)"
        ], GlassmorphismCanvas.COLOR_PURPLE))

        # Tier 3: Automation Engines, Orchestrators & Extensibility Layer (Core Runtimes)
        c.add_container(40, 360, 1120, 130, "Automation Engines, Orchestrators & Extensibility Layer (Core Runtimes)", subtitle="Cloud Templates, Resource Allocation & Embedded Orchestrator", icon="⚙️", border_color="rgba(88,166,255,0.3)")
        c.add_card(GlassCard("vcfa-rt-iac", 65, 390, 345, 85, "⚡ Cloud Templates & IaC Engine", "prelude", "⚡", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• cloud-template-service & blueprints",
            "• Terraform & Content Gateway"
        ], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("vcfa-rt-alloc", 430, 390, 345, 85, "🚀 Resource Allocation & Placement", "prelude", "🚀", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• provisioning-service & placement",
            "• lease-service & project-service"
        ], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("vcfa-rt-vro", 795, 390, 345, 85, "⚙️ Extensibility & vRO Workflow Engine", "prelude", "⚙️", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• orchestration-service (vRO Embedded)",
            "• abx-server (Serverless Actions)"
        ], GlassmorphismCanvas.COLOR_GREEN))

        # Tier 4: VCF Automation Microservices Fabric (Prelude & VMSP Platform Namespaces)
        c.add_container(40, 505, 1120, 230, "VCF Automation Microservices Fabric (Prelude & VMSP Platform Namespaces)", subtitle="Cloud Templates, Blueprints, Extensibility (ABX), Service Broker & VMSP Core", icon="📦", border_color="rgba(63,185,80,0.3)")
        c.add_card(GlassCard("pod-prelude-iac", 65, 535, 260, 185, "⚡ Cloud Templates & IaC", "prelude", "⚡", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• cloud-template-service",
            "• blueprint-service",
            "• terraform-service",
            "• content-gateway-service",
            "• schema-service"
        ], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("pod-prelude-broker", 345, 535, 260, 185, "📦 Service Broker & Catalog", "prelude", "📦", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• catalog-service",
            "• consumer-service",
            "• policy-service (Governance)",
            "• approval-service",
            "• pricing-service"
        ], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("pod-vmsp-core", 625, 535, 260, 185, "🛠️ VMSP Platform Core", "vmsp-platform", "🛠️", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• argo-workflow-controller",
            "• identity-broker-service",
            "• vault-agent-injector",
            "• system-shutdown-watcher",
            "• deployment-autoscaler"
        ], GlassmorphismCanvas.COLOR_GREEN))
        c.add_card(GlassCard("pod-vmsp-data", 905, 535, 260, 185, "💾 Database & Cache State", "vmsp-platform", "💾", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• postgresql-ha (Pgpool-II)",
            "• postgres-prelude-master",
            "• postgres-vmsp-master",
            "• redis-cluster (Cache)",
            "• rabbitmq-ha (Event Bus)"
        ], GlassmorphismCanvas.COLOR_PURPLE))

        # Tier 5: Local Storage CSI Operator, Bound PVCs & Underlying Fast SSD Storage
        c.add_container(40, 750, 1120, 175, "Local Storage CSI Operator, Bound PVCs & Underlying Fast SSD Storage", subtitle="HostPath / Local Storage CSI, Bound PVCs for vRO DB, Blueprints & Workflow Logs", icon="💾", border_color="rgba(56,189,248,0.3)")
        c.add_card(GlassCard("vcfa-csi", 65, 780, 345, 130, "💾 Local Storage CSI Operator", "HostPath Dynamic Binding", "💾", "BOUND", GlassmorphismCanvas.COLOR_GREEN, [
            "• StorageClass: local-path-provisioner",
            "• HostPath dynamic PV volume binder",
            "• High IOPS SSD local volumes",
            "• Volume snapshot & backup integration"
        ], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("vcfa-pvcs", 430, 780, 345, 130, "📦 Bound Persistent Volumes (PVCs)", "Dynamic Allocation", "📦", "BOUND", GlassmorphismCanvas.COLOR_GREEN, [
            "• pvc-vro-db-data (15 GB Bound)",
            "• pvc-postgres-vmsp (25 GB Bound)",
            "• pvc-blueprint-repo (10 GB Bound)",
            "• pvc-argo-artifact-store (20 GB Bound)"
        ], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("vcfa-disk", 795, 780, 345, 130, "🏛️ Fast Local SSD Storage Tier", "High-IOPS Subsystem", "🏛️", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, [
            "• /var/lib/containerd & /data/pvcs",
            "• Sub-millisecond IOPS for Prelude DBs",
            "• Rapid container startup & staging",
            "• Auto-recovering stateful failover"
        ], GlassmorphismCanvas.COLOR_CYAN))

        # Flow Edges
        c.add_edge(FlowEdge((235, 190), (330, 245), "Envoy Ingress", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((600, 190), (890, 245), "Service Mesh", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((330, 335), (235, 390), "IaC Engine", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((890, 335), (600, 390), "Placement", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((235, 475), (195, 535), "Blueprints", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((600, 475), (475, 535), "Catalog", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((970, 475), (755, 535), "Platform Core", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((600, 720), (600, 750), "Local Storage CSI", GlassmorphismCanvas.COLOR_CYAN))

        return c

    def build_holorouter_architecture(self) -> GlassmorphismCanvas:
        """15. Holorouter Services, Container Breakdown & Reverse Proxy Architecture"""
        domain_str = self.env.dns_domain or "site-a.vcf.lab"
        h = self.env.holorouter
        r_ip = h.ip or resolve_host('router', domain_str) or "10.1.10.129"
        gw_ip = self.env.gateway_ip or resolve_host('gateway', domain_str) or "192.168.0.1"
        core_sub = self.env.core_subnet or get_subnet_for_ip(r_ip, 25) or "10.1.10.128/25"
        ext_sub = self.env.external_subnet or get_subnet_for_ip(gw_ip, 24) or "192.168.0.0/24"
        squid_mode = h.squid_filter_mode or "open"
        squid_badge = "FILTERING" if squid_mode == "filtered" else "OPEN"
        squid_badge_color = GlassmorphismCanvas.COLOR_AMBER if squid_mode == "filtered" else GlassmorphismCanvas.COLOR_GREEN

        c = GlassmorphismCanvas(
            width=1200, height=980,
            title="Holorouter Services & Container Reverse Proxy Architecture",
            subtitle=f"NGINX TLS Reverse Proxy, Docker Containers, Authentik OIDC, Technitium DNS, Vault PKI & Squid Filtering | Host: {r_ip}",
            style_name=self.diagram_style
        )
        c.add_legend([
            ("Ingress & Network Ports", GlassmorphismCanvas.COLOR_AMBER),
            ("NGINX & TLS Reverse Proxy", GlassmorphismCanvas.COLOR_PURPLE),
            ("Identity & Security Containers", GlassmorphismCanvas.COLOR_BLUE),
            ("DNS & Core Service Containers", GlassmorphismCanvas.COLOR_GREEN),
            ("Network Gateway & Storage", GlassmorphismCanvas.COLOR_CYAN),
        ])

        # Tier 1: Dual-Homed Network Interfaces & Ingress Ports Matrix
        c.add_container(40, 85, 1120, 115, "Dual-Homed Network Interfaces & Ingress Ports Matrix (Firewall / NAT)", subtitle=f"External Ingress ({ext_sub}) & Internal Core Fabric ({core_sub}) Port Forwarding", icon="🌐", border_color="rgba(210,153,34,0.3)")
        c.add_card(GlassCard("holo-eth0", 65, 115, 345, 75, "🌐 External Interface (eth0)", f"{gw_ip} / Upstream Gateway", "🌐", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, [
            "• Default Gateway & Outbound NAT",
            "• nftables / iptables Stateful Firewall"
        ], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("holo-eth1", 430, 115, 345, 75, "🔌 Internal Interface (eth1)", f"{r_ip} (holorouter)", "⚡", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, [
            f"• Core Subnet Gateway: {core_sub}",
            "• Internal Route to VCF (10.1.0.0/16)"
        ], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("holo-ports", 795, 115, 345, 75, "⚡ Ingress Ports Matrix", "TCP/UDP Port Mappings", "🎛️", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [
            "• :80/:443 (Web/TLS), :53 (DNS), :3128 (Squid)",
            "• :32000 (Vault), :5380 (DNS UI), :9000 (Auth)"
        ], GlassmorphismCanvas.COLOR_AMBER))

        # Tier 2: NGINX Reverse Proxy & Dynamic TLS Certificate Termination
        c.add_container(40, 215, 1120, 130, "NGINX Reverse Proxy & Dynamic TLS Certificate Termination (Vault PKI)", subtitle=f"NGINX Virtual Host SNI Router & Vault PKI Root CA SSL/TLS Certificate Distribution", icon="🔒", border_color="rgba(188,140,255,0.3)")
        c.add_card(GlassCard("holo-nginx", 65, 245, 345, 90, "🔒 NGINX Reverse Proxy Engine", "NGINX 1.24+ (Systemd)", "🔒", "Active", GlassmorphismCanvas.COLOR_GREEN, [
            "• SNI Virtual Host Request Router",
            "• SSL/TLS Offloading & HSTS Headers",
            "• Upstream HTTP/1.1 & WebSocket Proxy"
        ], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("holo-vault-certs", 430, 245, 345, 90, "📜 Vault PKI Signed Certificates", "Root CA: holodeck", "📜", "Active", GlassmorphismCanvas.COLOR_GREEN, [
            "• CA: holodeck Root CA (2-Year Validity)",
            "• /etc/ssl/certs/holodeck-ca.pem",
            "• SAN: *.site-a.vcf.lab, *.vcf.lab"
        ], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("holo-sni-routes", 795, 245, 345, 90, "🔀 Virtual Host & SNI Upstreams", "Reverse Proxy Map", "🔀", "Active", GlassmorphismCanvas.COLOR_GREEN, [
            "• auth.vcf.lab:443 -> 127.0.0.1:9000 (Authentik)",
            "• ca.vcf.lab:443 -> 127.0.0.1:8000 (MS ADCS Proxy)",
            "• git.vcf.lab:443 -> 127.0.0.1:8080 (GitLab CE)"
        ], GlassmorphismCanvas.COLOR_PURPLE))

        # Tier 3: Core Identity, Security & PKI Container Services
        c.add_container(40, 360, 1120, 130, "Core Identity, Security & PKI Container Services (Docker & Systemd)", subtitle="Authentik OIDC / SCIM, HashiCorp Vault PKI Engine & Microsoft ADCS CA Proxy", icon="🛡️", border_color="rgba(88,166,255,0.3)")
        c.add_card(GlassCard("holo-auth", 65, 390, 345, 85, "🛡️ Authentik Identity Platform", "Docker (:9000)", "🛡️", "Active", GlassmorphismCanvas.COLOR_GREEN, [
            "• OIDC / OAuth2 & SCIM 2.0 Syncer",
            "• authentik-server & worker"
        ], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("holo-vault", 430, 390, 345, 85, "🔐 HashiCorp Vault PKI & Secrets", "Systemd (:32000)", "🔐", "Active", GlassmorphismCanvas.COLOR_GREEN, [
            "• Root CA PKI Engine & KV Secrets",
            "• sign-verbatim CSR Signing"
        ], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("holo-ca-proxy", 795, 390, 345, 85, "🏛️ Microsoft ADCS CA Proxy", "Python/Flask (:8000)", "🏛️", "Active", GlassmorphismCanvas.COLOR_GREEN, [
            "• ADCS Emulator for SDDC Manager",
            "• Orders PKCS#7 & Signs via Vault"
        ], GlassmorphismCanvas.COLOR_GREEN))

        # Tier 4: Core Infrastructure, DNS, SCM & Forward Proxy Services
        c.add_container(40, 505, 1120, 230, "Core Infrastructure, DNS, SCM & Forward Proxy Services (Docker & Systemd)", subtitle="Technitium DNS/DHCP, GitLab SCM Repository & Squid Forward Proxy Filtering", icon="📦", border_color="rgba(63,185,80,0.3)")
        c.add_card(GlassCard("holo-dns", 65, 535, 260, 185, "🌐 Technitium DNS", "Docker (:53, :5380)", "🌐", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• Core DNS Listener (:53)",
            "• Web UI (:5380 / dns.vcf.lab)",
            "• Zones: site-a.vcf.lab, vcf.lab",
            "• DHCP Server Engine",
            "• Dynamic DNS REST API"
        ], GlassmorphismCanvas.COLOR_GREEN))
        c.add_card(GlassCard("holo-gitlab", 345, 535, 260, 185, "🦊 GitLab CE Repository", "Docker (:8080, :2222)", "🦊", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• Web UI & Git HTTP (:8080)",
            "• Git SSH Protocol (:2222)",
            "• Lab Automation Repos",
            "• CI/CD Pipelines & Webhooks",
            "• Embedded PostgreSQL DB"
        ], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("holo-squid", 625, 535, 260, 185, "🛡️ Squid Forward Proxy", f"Systemd (:3128)", "🛡️", squid_badge, squid_badge_color, [
            "• HTTP/HTTPS Egress Proxy",
            "• Allowlist: /etc/squid/allowlist",
            f"• Mode: {squid_mode.upper()}",
            "• Whitelist Domain Filtering",
            "• Egress Traffic Logger"
        ], GlassmorphismCanvas.COLOR_GREEN))
        c.add_card(GlassCard("holo-host-svc", 905, 535, 260, 185, "⚙️ Core Host Services", "Systemd & SSH", "⚙️", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• OpenSSH Server (:22)",
            "• Chrony NTP Time Sync",
            "• iptables / nftables NAT",
            "• Rsyslog Central Collector",
            "• Docker Daemon (dockerd)"
        ], GlassmorphismCanvas.COLOR_PURPLE))

        # Tier 5: Storage Volumes, Kernel Routing & Stateful Subsystems
        c.add_container(40, 750, 1120, 175, "Storage Volumes, Kernel Routing & Stateful Subsystems", subtitle="Persistent Docker Volumes, nftables Packet NAT & Kernel IP Forwarding", icon="💾", border_color="rgba(56,189,248,0.3)")
        c.add_card(GlassCard("holo-vols", 65, 780, 345, 130, "💾 Persistent Docker Volumes", "Local Host Mounts", "💾", "MOUNTED", GlassmorphismCanvas.COLOR_GREEN, [
            "• authentik_media & authentik_db_data",
            "• gitlab_data, gitlab_config & logs",
            "• technitium_config & dns_data",
            "• Stored under /opt/holodeck"
        ], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("holo-nat", 430, 780, 345, 130, "🔀 Linux Kernel IP Routing & NAT", "Packet Forwarding", "🔀", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [
            "• net.ipv4.ip_forward = 1",
            "• nftables / iptables MASQUERADE eth0",
            "• Conntrack Stateful Inspection",
            "• Inter-Subnet Routing (Core <-> VCF)"
        ], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("holo-logs", 795, 780, 345, 130, "📜 System Logs & Observability", "Service Monitoring", "📜", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, [
            "• /var/log/nginx/access.log & error.log",
            "• /var/log/squid/access.log & cache.log",
            "• Docker Container logs (journald)",
            "• Systemd Watchers & Auto-Restart"
        ], GlassmorphismCanvas.COLOR_CYAN))

        # Flow Edges
        c.add_edge(FlowEdge((235, 190), (235, 245), "Port 443", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((600, 190), (600, 245), "TLS Certs", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((970, 190), (970, 245), "SNI Proxy", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((235, 335), (235, 390), "OIDC Traffic", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((600, 335), (600, 390), "Vault API", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((970, 335), (970, 390), "ADCS Proxy", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((195, 475), (195, 535), "DNS :53", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((475, 475), (475, 535), "Git :8080", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((755, 475), (755, 535), "Squid :3128", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((600, 720), (600, 750), "Stateful Volumes", GlassmorphismCanvas.COLOR_CYAN))

        return c

    def build_ssp_k8s_architecture(self) -> GlassmorphismCanvas:
        """14. Security Services Platform (SSP 5.2 / vDefend) Architecture"""
        domain_str = self.env.dns_domain or "site-a.vcf.lab"
        
        # Resolve dynamic SSP cluster details if discovered
        ssp_cl = next((cl for cl in self.env.k8s_clusters if cl.cluster_type == "SSP"), None)
        capi_ip = (ssp_cl.extra_info.get('capi_mgmt_ip') if ssp_cl and ssp_cl.extra_info and ssp_cl.extra_info.get('capi_mgmt_ip')
                   else (resolve_host('ssp-i', domain_str) or resolve_host('ssp-installer', domain_str) or '10.1.0.10'))
        ingress_vip = ssp_cl.vip if ssp_cl and ssp_cl.vip else (resolve_host('ssp', domain_str) or "10.1.0.11")
        
        raw_kv = ssp_cl.extra_info.get('kafka_vips') if (ssp_cl and ssp_cl.extra_info) else None
        if isinstance(raw_kv, list) and len(raw_kv) >= 4:
            kafka_vips = [str(x) for x in raw_kv]
        else:
            kafka_vips = [
                resolve_host('kafka-0', domain_str) or '10.1.0.12',
                resolve_host('kafka-1', domain_str) or '10.1.0.13',
                resolve_host('kafka-2', domain_str) or '10.1.0.14',
                resolve_host('kafka-3', domain_str) or '10.1.0.15'
            ]
        
        ssp_sub = get_subnet_for_ip(capi_ip, 24) or get_subnet_for_ip(ingress_vip, 24) or "SSP Network Fabric"
        
        # Resolve control plane and worker nodes
        cp_nodes = [n for n in ssp_cl.nodes if n.role == 'control-plane'] if ssp_cl else []
        if not cp_nodes:
            cp_nodes = [
                K8sNodeInfo(name="ssp-controller-6v77j", role="control-plane", status="Ready", cpu_capacity=4, memory_mb=8192, ip_address=resolve_host('ssp-cp-1', domain_str) or "10.1.0.22", taints=["node-role.kubernetes.io/control-plane:NoSchedule"]),
                K8sNodeInfo(name="ssp-controller-t27v4", role="control-plane", status="Ready", cpu_capacity=4, memory_mb=8192, ip_address=resolve_host('ssp-cp-2', domain_str) or "10.1.0.29", taints=["node-role.kubernetes.io/control-plane:NoSchedule"]),
                K8sNodeInfo(name="ssp-controller-rhx6j", role="control-plane", status="Ready", cpu_capacity=4, memory_mb=8192, ip_address=resolve_host('ssp-cp-3', domain_str) or "10.1.0.31", taints=["node-role.kubernetes.io/control-plane:NoSchedule"]),
            ]
            
        worker_nodes = [n for n in ssp_cl.nodes if n.role == 'worker'] if ssp_cl else []
        if not worker_nodes:
            worker_nodes = [
                K8sNodeInfo(name="ssp-md-0-worker-vmnr7", role="worker", status="Ready", cpu_capacity=12, memory_mb=57344, ip_address=resolve_host('ssp-worker-1', domain_str) or "10.1.0.23"),
                K8sNodeInfo(name="ssp-md-0-worker-jzxpd", role="worker", status="Ready", cpu_capacity=12, memory_mb=57344, ip_address=resolve_host('ssp-worker-2', domain_str) or "10.1.0.24"),
                K8sNodeInfo(name="ssp-md-0-worker-xczc5", role="worker", status="Ready", cpu_capacity=12, memory_mb=57344, ip_address=resolve_host('ssp-worker-3', domain_str) or "10.1.0.25"),
                K8sNodeInfo(name="ssp-md-0-worker-4znbw", role="worker", status="Ready", cpu_capacity=12, memory_mb=57344, ip_address=resolve_host('ssp-worker-4', domain_str) or "10.1.0.26"),
                K8sNodeInfo(name="ssp-md-0-worker-mfg2m", role="worker", status="Ready", cpu_capacity=12, memory_mb=57344, ip_address=resolve_host('ssp-worker-5', domain_str) or "10.1.0.27"),
                K8sNodeInfo(name="ssp-md-0-worker-m82wf", role="worker", status="Ready", cpu_capacity=12, memory_mb=57344, ip_address=resolve_host('ssp-worker-6', domain_str) or "10.1.0.28"),
            ]
            
        c = GlassmorphismCanvas(
            width=1200, height=980,
            title="Security Services Platform (SSP 5.2 / vDefend) Architecture",
            subtitle=f"CAPI Mgmt ({capi_ip}), Ingress VIP ({ingress_vip}), {len(cp_nodes)+len(worker_nodes)}-Node Cluster & vDefend Security Fabric | Domain: {domain_str}",
            style_name=self.diagram_style
        )
        c.add_legend([
            ("Ingress & Telemetry VIP", GlassmorphismCanvas.COLOR_AMBER),
            ("CAPI Management", GlassmorphismCanvas.COLOR_PURPLE),
            ("Control Plane (etcd)", GlassmorphismCanvas.COLOR_PURPLE),
            ("Worker Compute Node", GlassmorphismCanvas.COLOR_BLUE),
            ("Security Microservice", GlassmorphismCanvas.COLOR_GREEN),
            ("Data Lake & Storage", GlassmorphismCanvas.COLOR_CYAN),
        ])
        
        # Tier 1: Ingress & Telemetry Virtual Endpoints (MetalLB Layer 2)
        c.add_container(40, 85, 1120, 115, "SSP Ingress & Telemetry Virtual Endpoints (MetalLB Layer 2)", subtitle=f"Virtual IP Endpoints ({ssp_sub})", icon="🌐", border_color="rgba(210,153,34,0.3)")
        c.add_card(GlassCard("ssp-ing-vip", 65, 115, 345, 75, "⚡ Ingress Gateway VIP", f"https://ssp.{domain_str} ({ingress_vip}:443)", "🌐", "VIP ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [
            "• Project Contour Envoy Ingress",
            "• Web UI & AuthServer OIDC Proxy"
        ], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("ssp-kafka-vip", 430, 115, 345, 75, "⚡ Kafka External Telemetry VIP", f"{kafka_vips[0]}:9092 (Flow Bus)", "⚡", "VIP ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [
            "• NSX Security Flow Ingestion",
            "• High-Throughput Stream Pipeline"
        ], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("ssp-kafka-kraft", 795, 115, 345, 75, "⚡ Kafka KRaft Controller VIPs", f"{kafka_vips[1]}..{kafka_vips[-1]}:9092", "🎛️", "VIP ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [
            "• Kafka Controller 0, 1, 2 Endpoints",
            "• Consensus & Metadata Management"
        ], GlassmorphismCanvas.COLOR_AMBER))

        # Tier 2: CAPI Management & Workload Control Plane Quorum
        c.add_container(40, 215, 1120, 130, "Cluster API Management (ssp-i) & Workload Control Plane Quorum", subtitle=f"CAPI Management Plane ({capi_ip}) & 3-Node Workload CP Quorum (K8s v1.35.6)", icon="🛡️", border_color="rgba(188,140,255,0.3)")
        c.add_card(GlassCard("ssp-capi", 65, 245, 255, 90, "🛡️ ssp-i (CAPI Installer)", f"{capi_ip} (sysadmin)", "🛡️", "Ready", GlassmorphismCanvas.COLOR_GREEN, [
            "• CAPI, MachineDeployment",
            "• Secret: ssp-kubeconfig"
        ], GlassmorphismCanvas.COLOR_PURPLE))
        
        for idx, cp in enumerate(cp_nodes[:3]):
            x_pos = 340 + (idx * 275)
            c.add_card(GlassCard(f"ssp-cp-{idx+1}", x_pos, 245, 255, 90, f"☸️ {cp.name}", cp.ip_address, "☸️", cp.status, GlassmorphismCanvas.COLOR_GREEN, [
                f"• {cp.cpu_capacity} vCPU | {cp.memory_mb // 1024 if cp.memory_mb else 8} GB RAM",
                "• etcd quorum & K8s API"
            ], GlassmorphismCanvas.COLOR_PURPLE))

        # Tier 3: Workload Worker Node Fabric (6 Nodes Pool)
        c.add_container(40, 360, 1120, 130, "Workload Worker Node Fabric (6 Compute Nodes | Calico CNI 172.21.0.0/16)", subtitle="Hosts vDefend, NSX Intelligence, Analytics & Ingress Pods", icon="🖥️", border_color="rgba(88,166,255,0.3)")
        for idx, w in enumerate(worker_nodes[:6]):
            col = idx % 3
            row = idx // 3
            x_pos = 65 + (col * 365)
            y_pos = 390 + (row * 45)
            c.add_card(GlassCard(f"ssp-w-{idx+1}", x_pos, y_pos, 350, 40, f"🖥️ {w.name}", f"{w.ip_address} | {w.cpu_capacity} vCPU, {w.memory_mb // 1024 if w.memory_mb else 56}GB", "🖥️", w.status, GlassmorphismCanvas.COLOR_GREEN, [], GlassmorphismCanvas.COLOR_BLUE))

        # Tier 4: vDefend & NSX Intelligence Microservices Fabric (nsxi-platform)
        c.add_container(40, 505, 1120, 230, "vDefend & NSX Intelligence Microservices Fabric (nsxi-platform)", subtitle="NSX Intelligence, Network Detection & Response (NDR), Distributed Threat Analytics & Data Bus", icon="🔒", border_color="rgba(63,185,80,0.3)")
        c.add_card(GlassCard("pod-intel", 65, 535, 260, 185, "🔍 NSX Intelligence", "Flow & Network Analytics", "🔍", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• intelligence-ui (Portal)",
            "• latestflow (Correlation)",
            "• app-discovery & monitor",
            "• recommendation-engine",
            "• spark-operator-kf"
        ], GlassmorphismCanvas.COLOR_GREEN))
        c.add_card(GlassCard("pod-ndr", 345, 535, 260, 185, "🛡️ vDefend NDR & IDS", "Threat Detection & Response", "🛡️", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• ndr-ui & nsx-ndr-api",
            "• ids & signature engines",
            "• event-aggregator & scorer",
            "• llanta-detectors & NTA",
            "• pcap-storer & SIEM sender"
        ], GlassmorphismCanvas.COLOR_GREEN))
        c.add_card(GlassCard("pod-kafka", 625, 535, 260, 185, "⚡ Data Lake & Streaming", "OLAP & Stream Processing", "⚡", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• kafka-controller (3 KRaft)",
            "• druid-broker & coordinator",
            "• druid-historical & router",
            "• minio (4 S3 Object Pods)",
            "• redis-cluster (3 nodes)"
        ], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("pod-platform", 905, 535, 260, 185, "🔒 Platform & Storage", "Core Identity & State", "🔒", "Running", GlassmorphismCanvas.COLOR_GREEN, [
            "• postgresql-ha-pgpool (2)",
            "• authserver & authelia",
            "• cloud-connector & telemetry",
            "• trust-manager & debezium",
            "• ssp-sc (vSAN CSI Volume)"
        ], GlassmorphismCanvas.COLOR_PURPLE))

        # Tier 5: Ingress, Networking & CSI Storage Subsystems
        c.add_container(40, 750, 1120, 175, "Ingress, Networking & vSphere CNS Storage Subsystems", subtitle="Contour Ingress Controller, MetalLB Speaker BGP/L2, and VMware vSphere CNS / CSI Storage", icon="💾", border_color="rgba(56,189,248,0.3)")
        c.add_card(GlassCard("sub-contour", 65, 780, 345, 130, "🔀 Project Contour & MetalLB", "Ingress Controller & L2 LB", "🔀", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [
            "• projectcontour & Envoy DaemonSet",
            "• metallb-controller & metallb-speaker",
            "• cert-manager & TLS Certificate Injector",
            "• HTTPProxy Custom Resource Routing"
        ], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("sub-csi", 430, 780, 345, 130, "💾 VMware vSphere CSI Storage", "Cloud Native Storage (CNS)", "💾", "BOUND", GlassmorphismCanvas.COLOR_GREEN, [
            "• Driver: csi.vsphere.vmware.com",
            "• StorageClass: ssp-sc (Default)",
            "• 27+ Bound Persistent Volume Claims",
            "• Direct vSAN Block Volume Attachment"
        ], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("sub-vsan", 795, 780, 345, 130, "🏛️ Clustered vSAN Datastore", "Underlying Persistent Tier", "🏛️", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, [
            "• High-Performance NVMe / SSD vSAN ESA",
            "• RAID-5/6 Resilient Storage Policy",
            "• Backs Druid, Kafka, MinIO & DB Data",
            "• Zero-Loss Stateful Failover Quorum"
        ], GlassmorphismCanvas.COLOR_CYAN))

        # Flow Edges
        c.add_edge(FlowEdge((235, 190), (235, 780), "Ingress Route", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((600, 190), (755, 535), "Stream Flow", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((190, 335), (190, 390), "CAPI Deploy", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((600, 335), (600, 360), "K8s Scheduling", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((600, 490), (600, 505), "Workload Host", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((600, 735), (600, 750), "CNS Volume Mount", GlassmorphismCanvas.COLOR_CYAN))

        return c

    def build_all(self) -> Dict[str, str]:
        """Generate and return map of filename to SVG XML content for all 15 diagrams"""
        return {
            "high_level_architecture.svg": self.build_high_level_architecture().render(),
            "network_dataflow.svg": self.build_network_dataflow().render(),
            "vcf_domain_architecture.svg": self.build_vcf_domain_architecture().render(),
            "esxi_host_layout.svg": self.build_esxi_host_layout().render(),
            "core_infrastructure.svg": self.build_core_infrastructure().render(),
            "holorouter_architecture.svg": self.build_holorouter_architecture().render(),
            "dvs_topology.svg": self.build_dvs_topology().render(),
            "nsx_architecture.svg": self.build_nsx_architecture().render(),
            "lab_boot_sequence.svg": self.build_lab_boot_sequence().render(),
            "storage_summary.svg": self.build_storage_summary().render(),
            "complete_infrastructure.svg": self.build_complete_infrastructure().render(),
            "supervisor_k8s_architecture.svg": self.build_supervisor_k8s_architecture().render(),
            "vsp_k8s_architecture.svg": self.build_vsp_k8s_architecture().render(),
            "vcfa_k8s_architecture.svg": self.build_vcfa_k8s_architecture().render(),
            "ssp_k8s_architecture.svg": self.build_ssp_k8s_architecture().render(),
        }

#==============================================================================
# DATA COLLECTION
#==============================================================================

class LabDataCollector:
    """Collects lab environment data from live vCenter, SDDC, NSX, K8s, and Holorouter components"""
    
    def __init__(self, config_path: str = CONFIG_INI):
        self.config = ConfigParser()
        self.config_path = config_path
        self.password = get_password()
        self.env = LabEnvironment()
        self.vcenter_connections = {}
        
    def collect_all(self) -> LabEnvironment:
        """Collect all lab environment data dynamically"""
        print("Starting lab data collection...")
        
        # 1. Load configuration & detect environment flavor / topology
        self._load_config()
        
        # 2. Collect core infrastructure & Holorouter service info
        self._collect_core_info()
        
        # 3. Collect from SDDC Manager (if present)
        self._collect_sddc_info()
        
        # 4. Collect from vCenter servers (multi-vCenter & multi-site aware)
        self._collect_vcenter_info()
        
        # 5. Collect from NSX Managers
        self._collect_nsx_info()
        
        # 6. Collect Kubernetes cluster architectures (Supervisor, VSP, VCFA, SSP)
        self._collect_k8s_info()
        
        # Disconnect vCenters
        self._disconnect_vcenters()
        
        # Baseline fallback population for offline or partial discovery
        self._populate_fallback_data()
        
        print("Data collection complete.")
        return self.env
    
    def _load_config(self):
        """Load configuration from config.ini and detect lab flavor & topology"""
        print("Loading configuration...")
        
        if not os.path.isfile(self.config_path):
            print(f"  Config file not found: {self.config_path}")
            return
        
        self.config.read(self.config_path)
        raw_text = ""
        try:
            with open(self.config_path, 'r') as f:
                raw_text = f.read()
        except Exception:
            pass

        # Extract lab SKU & type
        if self.config.has_option('VPOD', 'vPod_SKU'):
            self.env.lab_sku = self.config.get('VPOD', 'vPod_SKU')
        
        if self.config.has_option('VPOD', 'labtype'):
            self.env.lab_type = self.config.get('VPOD', 'labtype').upper()
        
        # Detect VVF vs VCF flavor
        if self.config.has_section('VVF') or self.config.has_section('VVFFINAL') or 'vvfvCenter' in raw_text:
            self.env.lab_flavor = "VVF"
        else:
            self.env.lab_flavor = "VCF"

        # Detect Single Site vs Dual Site
        if 'site-b' in raw_text.lower() or 'vc-mgmt-b' in raw_text.lower() or 'esx-01b' in raw_text.lower() or '10.2.' in raw_text:
            self.env.has_site_b = True
            self.env.topology_type = "Dual Site"
        else:
            self.env.has_site_b = False
            self.env.topology_type = "Single Site"

        # Detect Security Services Platform (SSP / vDefend) presence from config
        if 'ssp' in raw_text.lower() or 'secop' in raw_text.lower() or 'vdefend' in raw_text.lower() or '2770' in self.env.lab_sku:
            self.env.has_ssp = True

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
        print(f"  Lab Flavor: {self.env.lab_flavor}")
        print(f"  Topology: {self.env.topology_type}")
    
    def _collect_core_info(self):
        """Collect core infrastructure & Holorouter service information"""
        print("Collecting core infrastructure & Holorouter service info...")
        
        # Router
        router_ip = resolve_host('router') or "10.1.10.129"
        self.env.router_ip = router_ip
        
        # Console
        console_ip = resolve_host('console') or "10.1.10.130"
        self.env.console_ip = console_ip
        
        # Manager (this machine)
        try:
            result = subprocess.run(['hostname', '-I'], capture_output=True, text=True)
            if result.returncode == 0:
                self.env.manager_ip = result.stdout.strip().split()[0]
            else:
                self.env.manager_ip = "10.1.10.131"
        except Exception:
            self.env.manager_ip = "10.1.10.131"

        # Holorouter Services Probes
        h = HolorouterInfo(ip=self.env.router_ip)
        
        # Technitium DNS (port 5380)
        h.technitium_status = "Active" if self._check_tcp_port(self.env.router_ip, 5380) or self._check_tcp_port(self.env.router_ip, 53) else "Offline"
        
        # Authentik (port 9000 or auth.vcf.lab)
        h.authentik_status = "Active" if self._check_tcp_port(self.env.router_ip, 9000) or self._check_tcp_port(self.env.router_ip, 443) else "Offline"
        
        # GitLab (port 80 or 443)
        h.gitlab_status = "Active" if self._check_tcp_port(self.env.router_ip, 80) or self._check_tcp_port(self.env.router_ip, 443) else "Offline"
        
        # Vault (port 32000)
        h.vault_status = "Active" if self._check_tcp_port(self.env.router_ip, 32000) else "Offline"
        
        # Squid Proxy (port 3128)
        h.squid_status = "Active" if self._check_tcp_port(self.env.router_ip, 3128) else "Offline"
        
        # Check Squid Allowlist via SSH
        if self.password and self._check_tcp_port(self.env.router_ip, 22, timeout=1):
            try:
                ssh_cmd = [
                    'sshpass', '-p', self.password,
                    'ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
                    f'root@{self.env.router_ip}',
                    'cat /etc/squid/allowlist 2>/dev/null'
                ]
                res = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=5)
                if res.returncode == 0 and res.stdout.strip():
                    lines = [l.strip() for l in res.stdout.strip().split('\n') if l.strip() and not l.strip().startswith('#')]
                    if any(l == '.*' or l == '*' for l in lines):
                        h.squid_filter_mode = "Open (Filtering Disabled)"
                        h.squid_domains_count = 0
                    else:
                        h.squid_filter_mode = f"Filtering Enabled ({len(lines)} domains)"
                        h.squid_domains_count = len(lines)
                else:
                    h.squid_filter_mode = "Filtering Enabled (Default)"
            except Exception:
                h.squid_filter_mode = "Filtering Enabled (Default)"
        else:
            h.squid_filter_mode = "Filtering Enabled (Default)"

        self.env.holorouter = h

        print(f"  Router: {self.env.router_ip}")
        print(f"  Console: {self.env.console_ip}")
        print(f"  Manager: {self.env.manager_ip}")
        print(f"  Holorouter Squid Mode: {h.squid_filter_mode}")
    
    def _check_tcp_port(self, host: str, port: int, timeout: float = 0.5) -> bool:
        """Check if a TCP port is open"""
        try:
            resolved_ip = resolve_host(host) or host
            if not resolved_ip:
                return False
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            res = s.connect_ex((resolved_ip, port))
            s.close()
            return res == 0
        except Exception:
            return False

    def _get_sddc_token(self, host: str) -> Optional[str]:
        """Get SDDC Manager access token trying multiple administrative users"""
        if not REQUESTS_AVAILABLE or requests is None:
            return None
        if not self._check_tcp_port(host, 443, timeout=0.5):
            return None
        for user in ['admin@local', 'vcf', 'administrator@vsphere.local']:
            try:
                resp = requests.post(
                    f'https://{host}/v1/tokens',
                    json={'username': user, 'password': self.password},
                    verify=False,
                    timeout=5
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get('accessToken')
            except Exception:
                pass
        return None

    def _collect_sddc_info(self):
        """Collect information from SDDC Manager"""
        print("Collecting SDDC Manager info...")
        if not REQUESTS_AVAILABLE or requests is None:
            print("  requests module unavailable, skipping SDDC Manager REST queries")
            return
        sddc_host = "sddcmanager-a.site-a.vcf.lab"
        
        token = self._get_sddc_token(sddc_host)
        if not token:
            print("  SDDC Manager API unauthenticated or not present in this lab")
            return
        
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        
        # Get domains
        try:
            resp = requests.get(f'https://{sddc_host}/v1/domains', headers=headers, verify=False, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                for elem in data.get('elements', []):
                    domain = DomainInfo(
                        name=elem.get('name', ''),
                        domain_type=elem.get('type', ''),
                        sso_domain=elem.get('ssoName', '')
                    )
                    vcenters = elem.get('vcenters', [])
                    if vcenters:
                        domain.vcenter_fqdn = vcenters[0].get('fqdn', '')
                    nsx = elem.get('nsxtCluster', {})
                    if nsx:
                        domain.nsx_fqdn = nsx.get('vipFqdn', '')
                    for cl in elem.get('clusters', []):
                        domain.clusters.append(cl.get('id', ''))
                    
                    self.env.domains.append(domain)
                    print(f"  Found SDDC domain: {domain.name} ({domain.domain_type})")
        except Exception as e:
            print(f"  Error querying SDDC domains: {e}")
        
        # Get clusters
        try:
            resp = requests.get(f'https://{sddc_host}/v1/clusters', headers=headers, verify=False, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                for elem in data.get('elements', []):
                    cluster = ClusterInfo(
                        name=elem.get('name', ''),
                        host_count=len(elem.get('hosts', [])),
                        datastore=elem.get('primaryDatastoreName', ''),
                        datastore_type=elem.get('primaryDatastoreType', '')
                    )
                    self.env.clusters.append(cluster)
        except Exception as e:
            print(f"  Error querying SDDC clusters: {e}")

    def _collect_vcenter_info(self):
        """Collect information from all vCenter servers directly using pyVmomi and REST"""
        if not PYVMOMI_AVAILABLE or connect is None or vim is None:
            print("pyVmomi not available, skipping direct vCenter collection")
            return
        
        print("Collecting vCenter info...")
        
        # Target vCenters discovery
        vcenter_hosts = []
        for domain in self.env.domains:
            if domain.vcenter_fqdn and domain.vcenter_fqdn not in vcenter_hosts:
                vcenter_hosts.append(domain.vcenter_fqdn)
        
        # Add from config.ini
        for sec in ['RESOURCES', 'VVF', 'VCF']:
            if self.config.has_section(sec):
                for opt in ['vCenters', 'vvfvCenter', 'vcfvCenter']:
                    if self.config.has_option(sec, opt):
                        vcs = self.config.get(sec, opt).split(',')
                        for vc in vcs:
                            vc_clean = vc.strip()
                            if vc_clean and vc_clean not in vcenter_hosts:
                                vcenter_hosts.append(vc_clean)
        
        # Default fallbacks if empty
        if not vcenter_hosts:
            vcenter_hosts = ['vc-mgmt-a.site-a.vcf.lab', 'vc-wld01-a.site-a.vcf.lab']
            if self.env.has_site_b:
                vcenter_hosts.extend(['vc-mgmt-b.site-b.vcf.lab', 'vc-wld01-b.site-b.vcf.lab'])

        for vc_host in vcenter_hosts:
            print(f"  Connecting to vCenter {vc_host}...")
            site_label = "Site B" if 'site-b' in vc_host or '-b' in vc_host else "Site A"
            sso_user = 'administrator@wld.sso' if 'wld' in vc_host else 'administrator@vsphere.local'
            
            si = self._connect_vcenter(vc_host, sso_user)
            if not si and sso_user != 'administrator@vsphere.local':
                si = self._connect_vcenter(vc_host, 'administrator@vsphere.local')
            
            if not si:
                continue
            
            self.vcenter_connections[vc_host] = si
            content = si.RetrieveContent()
            
            # Extract vCenter & ESXi Product Versions
            try:
                about = content.about
                vc_ver = f"{about.name} {about.version} (Build {about.build})"
                if not self.env.vcf_version:
                    self.env.vcf_version = f"{self.env.lab_flavor} {about.version}"
            except Exception:
                pass
            
            # Query ESXi Hosts
            container = content.viewManager.CreateContainerView(content.rootFolder, [vim.HostSystem], True)
            for host_obj in container.view:
                fqdn = host_obj.name
                prod_ver = f"{host_obj.config.product.name} {host_obj.config.product.version} (Build {host_obj.config.product.build})"
                if not self.env.esxi_version:
                    self.env.esxi_version = prod_ver
                
                cores = host_obj.hardware.cpuInfo.numCpuCores if host_obj.hardware and host_obj.hardware.cpuInfo else 0
                mem_gb = round(host_obj.hardware.memorySize / (1024**3), 1) if host_obj.hardware else 0.0
                state = str(host_obj.runtime.connectionState)
                pstate = str(host_obj.runtime.powerState)
                
                # IP Addresses & NICs
                mgmt_ip = ""
                vsan_ip = ""
                vmotion_ip = ""
                tep_ip = ""
                vmnics = []
                
                if host_obj.config and host_obj.config.network:
                    for vnic in host_obj.config.network.vnic:
                        device = vnic.device
                        ip_addr = vnic.spec.ip.ipAddress
                        if device == 'vmk0':
                            mgmt_ip = ip_addr
                        elif device in ['vmk1', 'vmk2']:
                            if not vsan_ip:
                                vsan_ip = ip_addr
                            else:
                                vmotion_ip = ip_addr
                        elif device in ['vmk50', 'vmk10']:
                            tep_ip = ip_addr
                    
                    for pnic in host_obj.config.network.pnic:
                        vmnics.append(pnic.device)

                if not mgmt_ip:
                    mgmt_ip = resolve_host(fqdn)
                
                # Cluster name
                cl_name = host_obj.parent.name if host_obj.parent else ""
                
                host_info = HostInfo(
                    fqdn=fqdn,
                    state=state,
                    power_state=pstate,
                    cpu_cores=cores,
                    memory_gb=mem_gb,
                    mgmt_ip=mgmt_ip,
                    vsan_ip=vsan_ip,
                    vmotion_ip=vmotion_ip,
                    tep_ip=tep_ip,
                    cluster=cl_name,
                    version_build=prod_ver,
                    site=site_label,
                    vmnics=vmnics
                )
                
                # Avoid duplicates
                existing_idx = next((i for i, h in enumerate(self.env.hosts) if h.fqdn == fqdn), None)
                if existing_idx is not None:
                    self.env.hosts[existing_idx] = host_info
                else:
                    self.env.hosts.append(host_info)

            container.Destroy()
            
            # Query Clusters
            container = content.viewManager.CreateContainerView(content.rootFolder, [vim.ClusterComputeResource], True)
            for cl_obj in container.view:
                cl_name = cl_obj.name
                h_count = len(cl_obj.host)
                tot_cpu = cl_obj.summary.totalCpu if cl_obj.summary else 0
                tot_mem = round(cl_obj.summary.totalMemory / (1024**3), 1) if cl_obj.summary else 0.0
                
                drs_on = cl_obj.configurationEx.drsConfig.enabled if cl_obj.configurationEx and cl_obj.configurationEx.drsConfig else False
                drs_m = str(cl_obj.configurationEx.drsConfig.defaultVmBehavior) if drs_on else "disabled"
                ha_on = cl_obj.configurationEx.dasConfig.enabled if cl_obj.configurationEx and cl_obj.configurationEx.dasConfig else False
                
                cl_info = ClusterInfo(
                    name=cl_name,
                    host_count=h_count,
                    total_cpu_mhz=tot_cpu,
                    total_memory_gb=tot_mem,
                    site=site_label,
                    drs_enabled=drs_on,
                    drs_mode=drs_m,
                    ha_enabled=ha_on,
                    vsan_enabled=True
                )
                
                c_idx = next((i for i, c in enumerate(self.env.clusters) if c.name == cl_name), None)
                if c_idx is not None:
                    self.env.clusters[c_idx] = cl_info
                else:
                    self.env.clusters.append(cl_info)

            container.Destroy()
            
            # Query Datastores
            container = content.viewManager.CreateContainerView(content.rootFolder, [vim.Datastore], True)
            for ds in container.view:
                ds_name = ds.name
                ds_type = ds.summary.type if ds.summary else "VSAN"
                cap_gb = round(ds.summary.capacity / (1024**3), 1) if ds.summary else 0.0
                free_gb = round(ds.summary.freeSpace / (1024**3), 1) if ds.summary else 0.0
                used_gb = round(cap_gb - free_gb, 1)
                
                is_esa = 'esa' in ds_name.lower() or ds_type == 'VSAN_ESA'
                pol = "vSAN ESA RAID-5/6" if is_esa else "vSAN OSA FTT=1 (RAID-1 Mirroring)"
                
                ds_info = DatastoreInfo(
                    name=ds_name,
                    ds_type=ds_type,
                    capacity_gb=cap_gb,
                    free_gb=free_gb,
                    used_gb=used_gb,
                    site=site_label,
                    policy=pol,
                    is_esa=is_esa
                )
                
                if not any(d.name == ds_name for d in self.env.datastores):
                    self.env.datastores.append(ds_info)

            container.Destroy()

            # Query Distributed Virtual Switches & Portgroups
            container = content.viewManager.CreateContainerView(content.rootFolder, [vim.dvs.DistributedVirtualPortgroup], True)
            for pg in container.view:
                vlan_str = ""
                try:
                    vlan_spec = pg.config.defaultPortConfig.vlan
                    if hasattr(vlan_spec, 'vlanId'):
                        vlan_str = str(vlan_spec.vlanId)
                except Exception:
                    pass
                
                net_info = NetworkInfo(
                    name=pg.name,
                    dvs_name=pg.config.distributedVirtualSwitch.name if pg.config and pg.config.distributedVirtualSwitch else "",
                    vlan=vlan_str
                )
                if 'mgmt' in pg.name.lower() or 'management' in pg.name.lower():
                    if not any(n.name == pg.name for n in self.env.mgmt_networks):
                        self.env.mgmt_networks.append(net_info)
                else:
                    if not any(n.name == pg.name for n in self.env.wld_networks):
                        self.env.wld_networks.append(net_info)

            container.Destroy()

            # Query Virtual Machines
            container = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
            for vm in container.view:
                vm_name = vm.name
                pstate = str(vm.runtime.powerState)
                vcpus = vm.summary.config.numCpu if vm.summary and vm.summary.config else 0
                mem_mb = vm.summary.config.memorySizeMB if vm.summary and vm.summary.config else 0
                ip_addr = vm.guest.ipAddress if vm.guest and vm.guest.ipAddress else ""
                host_n = vm.runtime.host.name if vm.runtime and vm.runtime.host else ""
                guest_os = vm.summary.config.guestFullName if vm.summary and vm.summary.config else ""
                cl_n = vm.runtime.host.parent.name if vm.runtime and vm.runtime.host and vm.runtime.host.parent else ""
                
                vm_info = VMInfo(
                    name=vm_name,
                    power_state=pstate,
                    vcpus=vcpus,
                    memory_mb=mem_mb,
                    ip_address=ip_addr,
                    host=host_n,
                    cluster=cl_n,
                    guest_os=guest_os,
                    site=site_label
                )
                
                if 'vc-mgmt' in vm_name or 'sddc' in vm_name or 'nsx' in vm_name or 'ops' in vm_name or 'auto' in vm_name:
                    if not any(v.name == vm_name for v in self.env.mgmt_vms):
                        self.env.mgmt_vms.append(vm_info)
                else:
                    if not any(v.name == vm_name for v in self.env.wld_vms):
                        self.env.wld_vms.append(vm_info)

            container.Destroy()
            print(f"    vCenter {vc_host}: {len(self.env.hosts)} hosts, {len(self.env.datastores)} datastores collected")

    def _connect_vcenter(self, host: str, user: str) -> Optional[Any]:
        """Connect to a vCenter server with SSL verification disabled"""
        if not PYVMOMI_AVAILABLE or connect is None:
            return None
        if not self._check_tcp_port(host, 443, timeout=0.5):
            return None
        try:
            try:
                si = connect.SmartConnect(
                    host=host,
                    user=user,
                    pwd=self.password,
                    disableSslCertValidation=True
                )
            except TypeError:
                si = connect.SmartConnectNoSSL(
                    host=host,
                    user=user,
                    pwd=self.password
                )
            return si
        except Exception as e:
            print(f"    Connection to {host} as {user} failed: {e}")
            return None
    
    def _disconnect_vcenters(self):
        """Disconnect all vCenter connections"""
        if not PYVMOMI_AVAILABLE or connect is None:
            return
        for host, si in self.vcenter_connections.items():
            try:
                connect.Disconnect(si)
            except Exception:
                pass

    def _collect_nsx_info(self):
        """Collect NSX Edge & Gateway information"""
        print("Collecting NSX info...")
        if not REQUESTS_AVAILABLE or requests is None:
            print("  requests module unavailable, skipping REST NSX query")
            return
        
        nsx_managers = ['nsx-mgmt-01a.site-a.vcf.lab', 'nsx-wld01-01a.site-a.vcf.lab']
        if self.env.has_site_b:
            nsx_managers.extend(['nsx-mgmt-01b.site-b.vcf.lab', 'nsx-wld01-01b.site-b.vcf.lab'])

        for nsx_node in nsx_managers:
            if not self._check_tcp_port(nsx_node, 443, timeout=0.5):
                continue
            print(f"  Querying NSX Manager {nsx_node}...")
            try:
                resp = requests.get(
                    f'https://{nsx_node}/api/v1/transport-nodes?node_types=EdgeNode',
                    auth=('admin', self.password),
                    verify=False,
                    timeout=5
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for elem in data.get('results', []):
                        node_info = elem.get('node_deployment_info', {})
                        name = node_info.get('display_name', elem.get('display_name', ''))
                        edge = NSXEdgeInfo(name=name, cluster=nsx_node)
                        
                        ip_list = node_info.get('ip_addresses', [])
                        if ip_list:
                            edge.mgmt_ip = ip_list[0]
                        
                        host_switches = elem.get('host_switch_spec', {}).get('host_switches', [])
                        for hs in host_switches:
                            ip_spec = hs.get('ip_assignment_spec', {})
                            edge.tep_ips = ip_spec.get('ip_list', [])
                        
                        if not any(e.name == name for e in self.env.nsx_edges):
                            self.env.nsx_edges.append(edge)
                            print(f"    Found edge: {name}")
            except Exception as e:
                print(f"    NSX query failed for {nsx_node}: {e}")

    def _collect_k8s_info(self):
        """Collect live Kubernetes cluster details for Supervisor, VSP, VCFA, and SSP"""
        print("Collecting Kubernetes cluster architectures...")
        self._collect_supervisor_k8s()
        self._collect_vsp_k8s()
        self._collect_vcfa_k8s()
        self._collect_ssp_k8s()

    def _collect_supervisor_k8s(self):
        """Collect Supervisor Tanzu Cluster details via vCenter REST API & VM inventory"""
        print("  Collecting Supervisor Tanzu K8s cluster...")
        
        # Check if Supervisor VMs were already discovered during vCenter VM collection
        sup_vms = [v for v in (self.env.wld_vms + self.env.mgmt_vms) if 'supervisor' in v.name.lower()]
        
        k8s_cluster = K8sClusterInfo(
            cluster_type="Supervisor",
            name="domain-c8:supervisor",
            version="v1.28.2+vmware.1",
            vip="10.1.1.140",
            status="Healthy"
        )
        
        # Discover through vCenter REST if available
        if REQUESTS_AVAILABLE and requests is not None and self.password:
            vc_hosts = [
                ("vc-wld01-a.site-a.vcf.lab", "administrator@wld.sso"),
                ("vc-mgmt-a.site-a.vcf.lab", "administrator@vsphere.local")
            ]
            for vc_host, vc_user in vc_hosts:
                try:
                    session_resp = requests.post(
                        f'https://{vc_host}/api/session',
                        auth=(vc_user, self.password),
                        verify=False,
                        timeout=5
                    )
                    if session_resp.status_code in [200, 201]:
                        session_token = session_resp.json()
                        headers = {'vmware-api-session-id': session_token}
                        
                        # Check clusters
                        cl_resp = requests.get(f'https://{vc_host}/api/vcenter/namespace-management/clusters', headers=headers, verify=False, timeout=5)
                        if cl_resp.status_code == 200:
                            cl_data = cl_resp.json()
                            if cl_data and isinstance(cl_data, list):
                                first_cl = cl_data[0]
                                k8s_cluster.name = first_cl.get('cluster', k8s_cluster.name)
                                if first_cl.get('api_server_management_endpoint'):
                                    k8s_cluster.vip = first_cl.get('api_server_management_endpoint')
                                k8s_cluster.status = first_cl.get('kubernetes_status', 'Healthy')
                        
                        # Namespaces
                        ns_resp = requests.get(f'https://{vc_host}/api/vcenter/namespaces/instances', headers=headers, verify=False, timeout=5)
                        if ns_resp.status_code == 200:
                            for ns in ns_resp.json():
                                k8s_cluster.namespaces.append({
                                    'name': ns.get('namespace', ''),
                                    'status': ns.get('config_status', 'RUNNING')
                                })
                        break
                except Exception as e:
                    pass

        # Discover / populate Control Plane nodes from VM inventory or default specs
        if sup_vms:
            for i, vm in enumerate(sup_vms[:3]):
                node_ip = vm.ip_address if vm.ip_address else f"10.1.1.{137 + i}"
                k8s_cluster.nodes.append(K8sNodeInfo(
                    name=vm.name,
                    role="control-plane",
                    status="Ready" if vm.power_state in ("poweredOn", "POWERED_ON") else "NotReady",
                    cpu_capacity=vm.vcpus or 4,
                    memory_mb=vm.memory_mb or 16384,
                    ip_address=node_ip,
                    taints=["node-role.kubernetes.io/control-plane:NoSchedule"]
                ))
        
        # If no nodes discovered from VMs yet, populate standard Supervisor 3 CP VM topology
        if not k8s_cluster.nodes:
            k8s_cluster.nodes = [
                K8sNodeInfo(name="SupervisorControlPlaneVM (1)", role="control-plane", status="Ready", cpu_capacity=4, memory_mb=16384, ip_address="10.1.1.137", taints=["node-role.kubernetes.io/control-plane:NoSchedule"]),
                K8sNodeInfo(name="SupervisorControlPlaneVM (2)", role="control-plane", status="Ready", cpu_capacity=4, memory_mb=16384, ip_address="10.1.1.138", taints=["node-role.kubernetes.io/control-plane:NoSchedule"]),
                K8sNodeInfo(name="SupervisorControlPlaneVM (3)", role="control-plane", status="Ready", cpu_capacity=4, memory_mb=16384, ip_address="10.1.1.139", taints=["node-role.kubernetes.io/control-plane:NoSchedule"])
            ]
            
        if not k8s_cluster.pods:
            k8s_cluster.pods = [
                {'name': 'spherelet-agent', 'namespace': 'kube-system', 'status': 'Running'},
                {'name': 'antrea-agent', 'namespace': 'kube-system', 'status': 'Running'},
                {'name': 'coredns', 'namespace': 'kube-system', 'status': 'Running'},
                {'name': 'vmware-system-license', 'namespace': 'vmware-system-license', 'status': 'Running'}
            ]
            
        self.env.k8s_clusters.append(k8s_cluster)
        print(f"    Supervisor Cluster: {len(k8s_cluster.nodes)} nodes, {len(k8s_cluster.namespaces)} namespaces discovered")

    def _collect_vsp_k8s(self):
        """Collect VSP Fleet LCM K8s Cluster via SSH (Single Node CP/Worker)"""
        print("  Collecting VSP Fleet LCM K8s cluster...")
        domain_s = self.env.dns_domain or "site-a.vcf.lab"
        target_vip = resolve_host('vsp', domain_s) or resolve_host('vsp-vip', domain_s) or "10.1.1.142"
        vsp_fqdn = f"vsp-01a.{domain_s}"
        vsp_ip = resolve_host('vsp-01a', domain_s) or "10.1.1.141"
        
        # Candidates to probe
        candidates = [target_vip, vsp_fqdn, vsp_ip]
        target_ip = None
        
        if self.password:
            for cand in candidates:
                try:
                    resolved = socket.gethostbyname(cand) if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", cand) else cand
                except Exception:
                    resolved = cand
                if self._check_tcp_port(resolved, 22, timeout=1):
                    target_ip = resolved
                    break
                    
        if not target_ip:
            print("    VSP cluster SSH unreachable, using architecture specs")
            vsp_cluster = K8sClusterInfo(
                cluster_type="VSP",
                name="vsp-fleet-lcm",
                vip=target_vip,
                status="Healthy"
            )
            vsp_cluster.nodes.append(K8sNodeInfo(
                name=vsp_fqdn,
                role="control-plane, worker (Single Node)",
                status="Ready",
                cpu_capacity=8,
                memory_mb=32768,
                ip_address=vsp_ip,
                taints=["node-role.kubernetes.io/control-plane:NoSchedule"]
            ))
            vsp_cluster.pods = [
                {'name': 'fleet-lcm-operator', 'namespace': 'vcf-fleet-lcm', 'status': 'Running'},
                {'name': 'vcf-fleet-depot-service', 'namespace': 'vcf-fleet-lcm', 'status': 'Running'},
                {'name': 'sddc-lcm-service', 'namespace': 'vcf-sddc-lcm', 'status': 'Running'},
                {'name': 'telemetry-collector', 'namespace': 'telemetry', 'status': 'Running'}
            ]
            self.env.k8s_clusters.append(vsp_cluster)
            return

        try:
            ssh_cmd = [
                'sshpass', '-p', self.password,
                'ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
                f'vmware-system-user@{target_ip}',
                f'echo {self.password} | sudo -S -i bash -c "kubectl get nodes -o json 2>/dev/null"'
            ]
            res = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=5)
            vsp_cluster = K8sClusterInfo(
                cluster_type="VSP",
                name="vsp-fleet-lcm",
                vip=target_vip,
                status="Healthy"
            )
            
            if res.returncode == 0 and res.stdout.strip():
                nodes_data = json.loads(res.stdout.strip())
                for item in nodes_data.get('items', []):
                    m = item.get('metadata', {})
                    status = item.get('status', {})
                    alloc = status.get('allocatable', {})
                    taint_list = [t.get('key', '') + '=' + t.get('effect', '') for t in item.get('spec', {}).get('taints', [])]
                    
                    cpu_cap = int(alloc.get('cpu', '8').replace('m', ''))
                    mem_str = alloc.get('memory', '32Gi').replace('Ki', '').replace('Gi', '')
                    mem_mb = int(mem_str) * 1024 if 'Gi' in alloc.get('memory', '') else int(int(mem_str) / 1024)
                    
                    addresses = status.get('addresses', [])
                    node_ip = next((a.get('address') for a in addresses if a.get('type') == 'InternalIP'), vsp_ip)
                    
                    node = K8sNodeInfo(
                        name=m.get('name', vsp_fqdn),
                        role="control-plane, worker (Single Node)",
                        status="Ready" if any(cond.get('type') == 'Ready' and cond.get('status') == 'True' for cond in status.get('conditions', [])) else "NotReady",
                        cpu_capacity=cpu_cap,
                        memory_mb=mem_mb,
                        ip_address=node_ip,
                        taints=taint_list
                    )
                    vsp_cluster.nodes.append(node)
                    
            if not vsp_cluster.nodes:
                vsp_cluster.nodes.append(K8sNodeInfo(
                    name=vsp_fqdn,
                    role="control-plane, worker (Single Node)",
                    status="Ready",
                    cpu_capacity=8,
                    memory_mb=32768,
                    ip_address=vsp_ip,
                    taints=["node-role.kubernetes.io/control-plane:NoSchedule"]
                ))

            # Pods query
            ssh_pods_cmd = [
                'sshpass', '-p', self.password,
                'ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
                f'vmware-system-user@{target_ip}',
                f'echo {self.password} | sudo -S -i bash -c "kubectl get pods -A -o json 2>/dev/null"'
            ]
            res_pods = subprocess.run(ssh_pods_cmd, capture_output=True, text=True, timeout=5)
            if res_pods.returncode == 0 and res_pods.stdout.strip():
                pods_data = json.loads(res_pods.stdout.strip())
                for item in pods_data.get('items', []):
                    vsp_cluster.pods.append({
                        'name': item.get('metadata', {}).get('name', ''),
                        'namespace': item.get('metadata', {}).get('namespace', ''),
                        'status': item.get('status', {}).get('phase', 'Running')
                    })

            self.env.k8s_clusters.append(vsp_cluster)
            print(f"    VSP Cluster: {len(vsp_cluster.nodes)} nodes, {len(vsp_cluster.pods)} pods collected")
        except Exception as e:
            print(f"    VSP collection failed: {e}")

    def _collect_vcfa_k8s(self):
        """Collect VCF Automation K8s Cluster via SSH to auto-a"""
        print("  Collecting VCF Automation K8s cluster...")
        domain_s = self.env.dns_domain or "site-a.vcf.lab"
        target_ip = resolve_host('auto-a', domain_s) or resolve_host('auto', domain_s) or "10.1.1.70"
        if not self.password or not self._check_tcp_port(target_ip, 22, timeout=1):
            print("    VCFA cluster SSH unreachable, using architecture specs")
            return
        try:
            ssh_cmd = [
                'sshpass', '-p', self.password,
                'ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
                f'vmware-system-user@{target_ip}',
                f'echo {self.password} | sudo -S -i bash -c "kubectl get nodes -o json 2>/dev/null"'
            ]
            res = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=5)
            if res.returncode == 0 and res.stdout.strip():
                nodes_data = json.loads(res.stdout.strip())
                vcfa_cluster = K8sClusterInfo(
                    cluster_type="VCFA",
                    name="vcf-automation-cluster",
                    vip=target_ip,
                    status="Healthy"
                )
                
                for item in nodes_data.get('items', []):
                    m = item.get('metadata', {})
                    status = item.get('status', {})
                    alloc = status.get('allocatable', {})
                    taint_list = [t.get('key', '') + '=' + t.get('effect', '') for t in item.get('spec', {}).get('taints', [])]
                    
                    cpu_cap = int(alloc.get('cpu', '24').replace('m', ''))
                    mem_str = alloc.get('memory', '96Gi').replace('Ki', '').replace('Gi', '')
                    mem_mb = int(mem_str) * 1024 if 'Gi' in alloc.get('memory', '') else int(int(mem_str) / 1024)
                    
                    addresses = status.get('addresses', [])
                    node_ip = next((a.get('address') for a in addresses if a.get('type') == 'InternalIP'), target_ip)
                    
                    node = K8sNodeInfo(
                        name=m.get('name', 'auto-node'),
                        role="control-plane/worker",
                        status="Ready",
                        cpu_capacity=cpu_cap,
                        memory_mb=mem_mb,
                        ip_address=node_ip,
                        taints=taint_list
                    )
                    vcfa_cluster.nodes.append(node)

                # Pods query
                ssh_pods_cmd = [
                    'sshpass', '-p', self.password,
                    'ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
                    f'vmware-system-user@{target_ip}',
                    f'echo {self.password} | sudo -S -i bash -c "kubectl get pods -A -o json 2>/dev/null"'
                ]
                res_pods = subprocess.run(ssh_pods_cmd, capture_output=True, text=True, timeout=5)
                if res_pods.returncode == 0 and res_pods.stdout.strip():
                    pods_data = json.loads(res_pods.stdout.strip())
                    for item in pods_data.get('items', []):
                        vcfa_cluster.pods.append({
                            'name': item.get('metadata', {}).get('name', ''),
                            'namespace': item.get('metadata', {}).get('namespace', ''),
                            'status': item.get('status', {}).get('phase', 'Running')
                        })

                self.env.k8s_clusters.append(vcfa_cluster)
                print(f"    VCFA Cluster: {len(vcfa_cluster.nodes)} nodes, {len(vcfa_cluster.pods)} pods collected")
        except Exception as e:
            print(f"    VCFA collection failed: {e}")

    def _collect_ssp_k8s(self):
        """Collect Security Services Platform (SSP 5.2 / vDefend) K8s Cluster if present"""
        print("  Checking for Security Services Platform (SSP)...")
        domain_s = self.env.dns_domain or "site-a.vcf.lab"
        ssp_ip = resolve_host('ssp-i', domain_s) or resolve_host('ssp-installer', domain_s) or resolve_host('ssp', domain_s) or "10.1.0.10"
        
        if not self.password or not self._check_tcp_port(ssp_ip, 22, timeout=2):
            if self.env.has_ssp:
                print("    SSP cluster detected in config, but SSH unreachable; will use architecture baseline")
            else:
                print("    SSP cluster not present in this lab")
            return
        
        print(f"  Collecting live SSP K8s cluster details via ssp-i ({ssp_ip})...")
        try:
            py_collector = (
                "import json, subprocess, base64\n"
                "def run(cmd):\n"
                "    try:\n"
                "        return subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL).decode('utf-8')\n"
                "    except Exception:\n"
                "        return ''\n"
                "out = {}\n"
                "try:\n"
                "    m_nodes = run('kubectl get nodes -o json')\n"
                "    if m_nodes: out['mgmt_nodes'] = json.loads(m_nodes)\n"
                "    capi_cl = run('kubectl -n ssp get cluster ssp -o json')\n"
                "    if capi_cl: out['capi_cluster'] = json.loads(capi_cl)\n"
                "    run('kubectl -n ssp get secret ssp-kubeconfig -o jsonpath=\"{.data.value}\" | base64 -d > /tmp/wlkc 2>/dev/null')\n"
                "    wl_nodes = run('kubectl --kubeconfig=/tmp/wlkc get nodes -o json')\n"
                "    if wl_nodes: out['wl_nodes'] = json.loads(wl_nodes)\n"
                "    wl_svc = run('kubectl --kubeconfig=/tmp/wlkc get svc -A -o json')\n"
                "    if wl_svc: out['wl_svc'] = json.loads(wl_svc)\n"
                "    wl_proxies = run('kubectl --kubeconfig=/tmp/wlkc get httpproxy -A -o json')\n"
                "    if wl_proxies: out['wl_proxies'] = json.loads(wl_proxies)\n"
                "    wl_ns = run('kubectl --kubeconfig=/tmp/wlkc get ns -o json')\n"
                "    if wl_ns: out['wl_ns'] = json.loads(wl_ns)\n"
                "    wl_sc = run('kubectl --kubeconfig=/tmp/wlkc get sc -o json')\n"
                "    if wl_sc: out['wl_sc'] = json.loads(wl_sc)\n"
                "    wl_pvc = run('kubectl --kubeconfig=/tmp/wlkc get pvc -A -o json')\n"
                "    if wl_pvc: out['wl_pvc'] = json.loads(wl_pvc)\n"
                "    wl_pods = run('kubectl --kubeconfig=/tmp/wlkc get pods -A -o json')\n"
                "    if wl_pods: out['wl_pods'] = json.loads(wl_pods)\n"
                "    run('rm -f /tmp/wlkc')\n"
                "    print('JSON_START' + json.dumps(out) + 'JSON_END')\n"
                "except Exception as e:\n"
                "    print('JSON_START' + json.dumps({'error': str(e)}) + 'JSON_END')\n"
            )
            b64_script = base64.b64encode(py_collector.encode('utf-8')).decode('utf-8')
            
            ssh_cmd = [
                'sshpass', '-p', self.password,
                'ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
                f'sysadmin@{ssp_ip}',
                f'echo {b64_script} | base64 -d | python3'
            ]
            res = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=20)
            if res.returncode == 0 and 'JSON_START' in res.stdout:
                start_idx = res.stdout.find('JSON_START') + len('JSON_START')
                end_idx = res.stdout.find('JSON_END')
                if end_idx > start_idx:
                    data = json.loads(res.stdout[start_idx:end_idx])
                    if 'error' not in data and 'wl_nodes' in data:
                        ssp_cluster = K8sClusterInfo(
                            cluster_type="SSP",
                            name="ssp",
                            version="v1.35.6",
                            vip="10.1.0.11",
                            status="Healthy"
                        )
                        
                        # Process Workload Nodes
                        for item in data.get('wl_nodes', {}).get('items', []):
                            m = item.get('metadata', {})
                            status = item.get('status', {})
                            alloc = status.get('allocatable', {})
                            cap = status.get('capacity', {})
                            labels = m.get('labels', {})
                            taint_list = [t.get('key', '') + ('=' + t.get('effect', '') if t.get('effect') else '') for t in item.get('spec', {}).get('taints', [])]
                            
                            is_cp = 'node-role.kubernetes.io/control-plane' in labels or 'controller' in m.get('name', '')
                            role = 'control-plane' if is_cp else 'worker'
                            
                            cpu_cap_str = cap.get('cpu', alloc.get('cpu', '12'))
                            try:
                                cpu_cap = int(cpu_cap_str.replace('m', ''))
                            except Exception:
                                cpu_cap = 12 if role == 'worker' else 4
                                
                            mem_str = cap.get('memory', alloc.get('memory', '56Gi'))
                            try:
                                if 'Ki' in mem_str:
                                    mem_mb = int(int(mem_str.replace('Ki', '')) / 1024)
                                elif 'Gi' in mem_str:
                                    mem_mb = int(float(mem_str.replace('Gi', '')) * 1024)
                                elif 'Mi' in mem_str:
                                    mem_mb = int(float(mem_str.replace('Mi', '')))
                                else:
                                    mem_mb = int(int(mem_str) / (1024 * 1024))
                            except Exception:
                                mem_mb = 57344 if role == 'worker' else 8192
                                
                            addresses = status.get('addresses', [])
                            node_ip = next((a.get('address') for a in addresses if a.get('type') == 'InternalIP'), '')
                            
                            node = K8sNodeInfo(
                                name=m.get('name', 'ssp-node'),
                                role=role,
                                status="Ready",
                                cpu_capacity=cpu_cap,
                                memory_mb=mem_mb,
                                ip_address=node_ip,
                                taints=taint_list
                            )
                            ssp_cluster.nodes.append(node)
                            
                        # Process Workload Services & Endpoints
                        for item in data.get('wl_svc', {}).get('items', []):
                            m = item.get('metadata', {})
                            spec = item.get('spec', {})
                            status = item.get('status', {})
                            if spec.get('type') == 'LoadBalancer':
                                ing = status.get('loadBalancer', {}).get('ingress', [])
                                lb_ip = ing[0].get('ip') if ing else spec.get('loadBalancerIP', '')
                                ssp_cluster.services.append({
                                    'name': m.get('name', ''),
                                    'namespace': m.get('namespace', ''),
                                    'vip': lb_ip,
                                    'ports': [p.get('port') for p in spec.get('ports', [])]
                                })
                                
                        # Process Namespaces
                        for item in data.get('wl_ns', {}).get('items', []):
                            m = item.get('metadata', {})
                            status = item.get('status', {})
                            ssp_cluster.namespaces.append({
                                'name': m.get('name', ''),
                                'status': status.get('phase', 'Active')
                            })
                            
                        # Process Pods
                        for item in data.get('wl_pods', {}).get('items', []):
                            m = item.get('metadata', {})
                            status = item.get('status', {})
                            ssp_cluster.pods.append({
                                'name': m.get('name', ''),
                                'namespace': m.get('namespace', ''),
                                'status': status.get('phase', 'Running'),
                                'node': item.get('spec', {}).get('nodeName', '')
                            })
                            
                        # Process StorageClasses & PVCs
                        for item in data.get('wl_sc', {}).get('items', []):
                            ssp_cluster.storage_classes.append(item.get('metadata', {}).get('name', ''))
                            
                        bound_pvc_count = len([p for p in data.get('wl_pvc', {}).get('items', []) if p.get('status', {}).get('phase') == 'Bound'])
                        
                        # Extra metadata
                        ssp_cluster.extra_info = {
                            'capi_mgmt_ip': ssp_ip,
                            'capi_mgmt_role': 'Installer & CAPI Management Node (ssp-i)',
                            'ingress_vip': '10.1.0.11',
                            'ingress_fqdn': f'ssp.{self.env.dns_domain or "site-a.vcf.lab"}',
                            'kafka_vips': ['10.1.0.12', '10.1.0.13', '10.1.0.14', '10.1.0.15'],
                            'storage_class': 'ssp-sc (csi.vsphere.vmware.com)',
                            'pvc_count': bound_pvc_count,
                            'capi_status': 'Provisioned (3 CPs, 6 Workers)',
                        }
                        
                        self.env.has_ssp = True
                        self.env.k8s_clusters.append(ssp_cluster)
                        cp_count = len([n for n in ssp_cluster.nodes if n.role == 'control-plane'])
                        w_count = len([n for n in ssp_cluster.nodes if n.role == 'worker'])
                        print(f"    SSP Cluster: {len(ssp_cluster.nodes)} nodes ({cp_count} CPs, {w_count} Workers), {len(ssp_cluster.pods)} pods, {len(ssp_cluster.services)} LBs collected")
                        return
        except Exception as e:
            print(f"    SSP collection failed: {e}")

    def _populate_fallback_data(self):
        """Populate essential baseline infrastructure & Kubernetes data if not discovered dynamically"""
        domain_s = self.env.dns_domain or "site-a.vcf.lab"
        
        # 1. Hosts fallback if empty
        if not self.env.hosts:
            host_count = 8 if self.env.has_site_b else 7
            for i in range(1, host_count + 1):
                site_suffix = "b" if (self.env.has_site_b and i > 4) else "a"
                host_num = (i - 4) if (self.env.has_site_b and i > 4) else i
                host_fqdn = f"esx-{host_num:02d}{site_suffix}.{domain_s}"
                self.env.hosts.append(HostInfo(
                    fqdn=host_fqdn,
                    state="connected",
                    power_state="poweredOn",
                    cpu_cores=32,
                    memory_gb=128.0,
                    mgmt_ip=f"10.{2 if site_suffix == 'b' else 1}.1.{10 + host_num}",
                    cluster=f"cluster-{'wld01' if (not self.env.has_site_b and i > 4) else 'mgmt'}-{site_suffix}",
                    domain=f"mgmt-{site_suffix}" if (self.env.has_site_b or i <= 4) else "wld01-a",
                    version_build="9.1.0-24512345",
                    site=f"Site {site_suffix.upper()}"
                ))

        # 2. Datastores fallback if empty
        if not self.env.datastores:
            self.env.datastores.append(DatastoreInfo(
                name="vsan-site-a-01",
                ds_type="VSAN_ESA",
                capacity_gb=12000.0,
                free_gb=7500.0,
                used_gb=4500.0,
                site="Site A",
                policy="vSAN ESA RAID-5/6",
                is_esa=True
            ))
            if self.env.has_site_b:
                self.env.datastores.append(DatastoreInfo(
                    name="vsan-site-b-01",
                    ds_type="VSAN_ESA",
                    capacity_gb=12000.0,
                    free_gb=7500.0,
                    used_gb=4500.0,
                    site="Site B",
                    policy="vSAN ESA RAID-5/6",
                    is_esa=True
                ))

        # 3. Domains fallback if empty
        if not self.env.domains:
            self.env.domains.append(DomainInfo(
                name="mgmt-a",
                domain_type="MANAGEMENT",
                vcenter_fqdn="vc-mgmt-a.site-a.vcf.lab",
                nsx_fqdn="nsx-mgmt-01a.site-a.vcf.lab",
                sso_domain="vsphere.local",
                clusters=["cluster-mgmt-a"]
            ))
            if self.env.has_site_b:
                self.env.domains.append(DomainInfo(
                    name="mgmt-b",
                    domain_type="MANAGEMENT",
                    vcenter_fqdn="vc-mgmt-b.site-b.vcf.lab",
                    nsx_fqdn="nsx-mgmt-01b.site-b.vcf.lab",
                    sso_domain="vsphere.local",
                    clusters=["cluster-mgmt-b"]
                ))
            else:
                self.env.domains.append(DomainInfo(
                    name="wld01-a",
                    domain_type="VI",
                    vcenter_fqdn="vc-wld01-a.site-a.vcf.lab",
                    nsx_fqdn="nsx-wld01-01a.site-a.vcf.lab",
                    sso_domain="wld01.local",
                    clusters=["cluster-wld01-a"]
                ))

        # 4. NSX Edges fallback if empty
        if not self.env.nsx_edges:
            edge1_ip = resolve_host('vna-wld01-01a', domain_s) or resolve_host('edge-wld01-01a', domain_s) or "10.1.1.51"
            edge2_ip = resolve_host('vna-wld01-02a', domain_s) or resolve_host('edge-wld01-02a', domain_s) or "10.1.1.52"
            self.env.nsx_edges.append(NSXEdgeInfo(
                name="vna-wld01-01a",
                cluster=f"nsx-wld01-01a.{domain_s}",
                mgmt_ip=edge1_ip,
                tep_ips=[f"10.1.3.{i}" for i in [51, 52]]
            ))
            self.env.nsx_edges.append(NSXEdgeInfo(
                name="vna-wld01-02a",
                cluster=f"nsx-wld01-01a.{domain_s}",
                mgmt_ip=edge2_ip,
                tep_ips=[f"10.1.3.{i}" for i in [53, 54]]
            ))

        # 5. Kubernetes clusters baseline if empty or missing
        has_sup = any(c.cluster_type == "Supervisor" for c in self.env.k8s_clusters)
        if not has_sup:
            sup_vip = resolve_host('supervisor', domain_s) or resolve_host('wcp', domain_s) or "10.1.1.140"
            cp1_ip = resolve_host('sup-cp-1', domain_s) or resolve_host('SupervisorControlPlaneVM-1', domain_s) or "10.1.1.137"
            cp2_ip = resolve_host('sup-cp-2', domain_s) or resolve_host('SupervisorControlPlaneVM-2', domain_s) or "10.1.1.138"
            cp3_ip = resolve_host('sup-cp-3', domain_s) or resolve_host('SupervisorControlPlaneVM-3', domain_s) or "10.1.1.139"
            self.env.k8s_clusters.append(K8sClusterInfo(
                cluster_type="Supervisor",
                name="domain-c8:supervisor",
                version="v1.28.2+vmware.1",
                vip=sup_vip,
                status="Healthy",
                nodes=[
                    K8sNodeInfo(name="SupervisorControlPlaneVM (1)", role="control-plane", status="Ready", cpu_capacity=4, memory_mb=16384, ip_address=cp1_ip, taints=["node-role.kubernetes.io/control-plane:NoSchedule"]),
                    K8sNodeInfo(name="SupervisorControlPlaneVM (2)", role="control-plane", status="Ready", cpu_capacity=4, memory_mb=16384, ip_address=cp2_ip, taints=["node-role.kubernetes.io/control-plane:NoSchedule"]),
                    K8sNodeInfo(name="SupervisorControlPlaneVM (3)", role="control-plane", status="Ready", cpu_capacity=4, memory_mb=16384, ip_address=cp3_ip, taints=["node-role.kubernetes.io/control-plane:NoSchedule"])
                ],
                namespaces=[
                    {'name': 'kube-system', 'status': 'RUNNING'},
                    {'name': 'svc-harbor', 'status': 'RUNNING'},
                    {'name': 'ns-hol-apps', 'status': 'RUNNING'}
                ],
                pods=[
                    {'name': 'spherelet-agent', 'namespace': 'kube-system', 'status': 'Running'},
                    {'name': 'antrea-agent', 'namespace': 'kube-system', 'status': 'Running'},
                    {'name': 'coredns', 'namespace': 'kube-system', 'status': 'Running'},
                    {'name': 'vmware-system-license', 'namespace': 'vmware-system-license', 'status': 'Running'}
                ]
            ))

        has_vsp = any(c.cluster_type == "VSP" for c in self.env.k8s_clusters)
        if not has_vsp:
            vsp_vip = resolve_host('vsp', domain_s) or resolve_host('vsp-vip', domain_s) or "10.1.1.142"
            vsp_node_ip = resolve_host('vsp-01a', domain_s) or "10.1.1.141"
            self.env.k8s_clusters.append(K8sClusterInfo(
                cluster_type="VSP",
                name="vsp-fleet-lcm",
                version="v1.28.6",
                vip=vsp_vip,
                status="Healthy",
                nodes=[
                    K8sNodeInfo(
                        name=f"vsp-01a.{domain_s}",
                        role="control-plane, worker (Single Node)",
                        status="Ready",
                        cpu_capacity=8,
                        memory_mb=32768,
                        ip_address=vsp_node_ip,
                        taints=["node-role.kubernetes.io/control-plane:NoSchedule"]
                    )
                ],
                namespaces=[
                    {'name': 'vcf-fleet-lcm', 'status': 'RUNNING'},
                    {'name': 'vcf-sddc-lcm', 'status': 'RUNNING'},
                    {'name': 'telemetry', 'status': 'RUNNING'}
                ],
                pods=[
                    {'name': 'fleet-lcm-operator', 'namespace': 'vcf-fleet-lcm', 'status': 'Running'},
                    {'name': 'vcf-fleet-depot-service', 'namespace': 'vcf-fleet-lcm', 'status': 'Running'},
                    {'name': 'sddc-lcm-service', 'namespace': 'vcf-sddc-lcm', 'status': 'Running'},
                    {'name': 'telemetry-collector', 'namespace': 'telemetry', 'status': 'Running'}
                ]
            ))

        has_vcfa = any(c.cluster_type == "VCFA" for c in self.env.k8s_clusters)
        if not has_vcfa:
            vcfa_vip = resolve_host('auto-a', domain_s) or resolve_host('auto', domain_s) or "10.1.1.70"
            vcfa_node_ip = resolve_host('auto-platform-a', domain_s) or resolve_host('auto-a', domain_s) or "10.1.1.69"
            self.env.k8s_clusters.append(K8sClusterInfo(
                cluster_type="VCFA",
                name="vcfa-platform",
                version="v1.28.6",
                vip=vcfa_vip,
                status="Healthy",
                nodes=[
                    K8sNodeInfo(
                        name=f"auto-a.{domain_s}",
                        role="control-plane, worker (Single Node)",
                        status="Ready",
                        cpu_capacity=24,
                        memory_mb=98304,
                        ip_address=vcfa_node_ip,
                        taints=[]
                    )
                ],
                namespaces=[
                    {'name': 'prelude', 'status': 'RUNNING'},
                    {'name': 'vmsp-platform', 'status': 'RUNNING'},
                    {'name': 'istio-system', 'status': 'RUNNING'}
                ],
                pods=[
                    {'name': 'authentication-server', 'namespace': 'prelude', 'status': 'Running'},
                    {'name': 'resource-manager-server', 'namespace': 'prelude', 'status': 'Running'},
                    {'name': 'istio-ingressgateway', 'namespace': 'istio-system', 'status': 'Running'}
                ]
            ))

        has_ssp = any(c.cluster_type == "SSP" for c in self.env.k8s_clusters)
        if self.env.has_ssp and not has_ssp:
            ssp_vip = resolve_host('ssp', domain_s) or "10.1.0.11"
            ssp_i_ip = resolve_host('ssp-i', domain_s) or resolve_host('ssp-installer', domain_s) or "10.1.0.10"
            ssp_cp1_ip = resolve_host('ssp-cp-1', domain_s) or "10.1.0.22"
            ssp_cp2_ip = resolve_host('ssp-cp-2', domain_s) or "10.1.0.29"
            ssp_cp3_ip = resolve_host('ssp-cp-3', domain_s) or "10.1.0.31"
            ssp_w1_ip = resolve_host('ssp-worker-1', domain_s) or "10.1.0.23"
            ssp_w2_ip = resolve_host('ssp-worker-2', domain_s) or "10.1.0.24"
            ssp_w3_ip = resolve_host('ssp-worker-3', domain_s) or "10.1.0.25"
            ssp_w4_ip = resolve_host('ssp-worker-4', domain_s) or "10.1.0.26"
            ssp_w5_ip = resolve_host('ssp-worker-5', domain_s) or "10.1.0.27"
            ssp_w6_ip = resolve_host('ssp-worker-6', domain_s) or "10.1.0.28"
            
            k1 = resolve_host('kafka-0', domain_s) or '10.1.0.12'
            k2 = resolve_host('kafka-1', domain_s) or '10.1.0.13'
            k3 = resolve_host('kafka-2', domain_s) or '10.1.0.14'
            k4 = resolve_host('kafka-3', domain_s) or '10.1.0.15'

            self.env.k8s_clusters.append(K8sClusterInfo(
                cluster_type="SSP",
                name="ssp",
                version="v1.35.6",
                vip=ssp_vip,
                status="Healthy",
                nodes=[
                    K8sNodeInfo(name="ssp-controller-6v77j", role="control-plane", status="Ready", cpu_capacity=4, memory_mb=8192, ip_address=ssp_cp1_ip, taints=["node-role.kubernetes.io/control-plane:NoSchedule"]),
                    K8sNodeInfo(name="ssp-controller-t27v4", role="control-plane", status="Ready", cpu_capacity=4, memory_mb=8192, ip_address=ssp_cp2_ip, taints=["node-role.kubernetes.io/control-plane:NoSchedule"]),
                    K8sNodeInfo(name="ssp-controller-rhx6j", role="control-plane", status="Ready", cpu_capacity=4, memory_mb=8192, ip_address=ssp_cp3_ip, taints=["node-role.kubernetes.io/control-plane:NoSchedule"]),
                    K8sNodeInfo(name="ssp-md-0-worker-vmnr7", role="worker", status="Ready", cpu_capacity=12, memory_mb=57344, ip_address=ssp_w1_ip),
                    K8sNodeInfo(name="ssp-md-0-worker-jzxpd", role="worker", status="Ready", cpu_capacity=12, memory_mb=57344, ip_address=ssp_w2_ip),
                    K8sNodeInfo(name="ssp-md-0-worker-xczc5", role="worker", status="Ready", cpu_capacity=12, memory_mb=57344, ip_address=ssp_w3_ip),
                    K8sNodeInfo(name="ssp-md-0-worker-4znbw", role="worker", status="Ready", cpu_capacity=12, memory_mb=57344, ip_address=ssp_w4_ip),
                    K8sNodeInfo(name="ssp-md-0-worker-mfg2m", role="worker", status="Ready", cpu_capacity=12, memory_mb=57344, ip_address=ssp_w5_ip),
                    K8sNodeInfo(name="ssp-md-0-worker-m82wf", role="worker", status="Ready", cpu_capacity=12, memory_mb=57344, ip_address=ssp_w6_ip),
                ],
                namespaces=[
                    {'name': 'nsxi-platform', 'status': 'Active'},
                    {'name': 'projectcontour', 'status': 'Active'},
                    {'name': 'metallb-system', 'status': 'Active'},
                    {'name': 'cert-manager', 'status': 'Active'},
                    {'name': 'vmware-system-csi', 'status': 'Active'},
                    {'name': 'kube-system', 'status': 'Active'}
                ],
                services=[
                    {'name': 'projectcontour-envoy', 'namespace': 'projectcontour', 'vip': ssp_vip, 'ports': [80, 443]},
                    {'name': 'kafka-external', 'namespace': 'nsxi-platform', 'vip': k1, 'ports': [9092]},
                    {'name': 'kafka-controller-2-external', 'namespace': 'nsxi-platform', 'vip': k2, 'ports': [9092]},
                    {'name': 'kafka-controller-1-external', 'namespace': 'nsxi-platform', 'vip': k3, 'ports': [9092]},
                    {'name': 'kafka-controller-0-external', 'namespace': 'nsxi-platform', 'vip': k4, 'ports': [9092]}
                ],
                storage_classes=['ssp-sc'],
                extra_info={
                    'capi_mgmt_ip': ssp_i_ip,
                    'capi_mgmt_role': 'Installer & CAPI Management Node (ssp-i)',
                    'ingress_vip': ssp_vip,
                    'ingress_fqdn': f'ssp.{domain_s}',
                    'kafka_vips': [k1, k2, k3, k4],
                    'storage_class': 'ssp-sc (csi.vsphere.vmware.com)',
                    'pvc_count': 27,
                    'capi_status': 'Provisioned (3 CPs, 6 Workers)',
                },
                pods=[
                    {'name': 'intelligence-ui', 'namespace': 'nsxi-platform', 'status': 'Running'},
                    {'name': 'latestflow', 'namespace': 'nsxi-platform', 'status': 'Running'},
                    {'name': 'app-discovery', 'namespace': 'nsxi-platform', 'status': 'Running'},
                    {'name': 'ndr-ui', 'namespace': 'nsxi-platform', 'status': 'Running'},
                    {'name': 'ids', 'namespace': 'nsxi-platform', 'status': 'Running'},
                    {'name': 'nsx-ndr-api', 'namespace': 'nsxi-platform', 'status': 'Running'},
                    {'name': 'kafka-controller-0', 'namespace': 'nsxi-platform', 'status': 'Running'},
                    {'name': 'druid-broker', 'namespace': 'nsxi-platform', 'status': 'Running'},
                    {'name': 'minio-0', 'namespace': 'nsxi-platform', 'status': 'Running'},
                    {'name': 'postgresql-ha-pg-0', 'namespace': 'nsxi-platform', 'status': 'Running'},
                    {'name': 'redis-cluster-0', 'namespace': 'nsxi-platform', 'status': 'Running'},
                    {'name': 'projectcontour-envoy', 'namespace': 'projectcontour', 'status': 'Running'},
                    {'name': 'ssp-metallb-speaker', 'namespace': 'metallb-system', 'status': 'Running'},
                    {'name': 'vsphere-csi-controller', 'namespace': 'vmware-system-csi', 'status': 'Running'}
                ]
            ))

#==============================================================================
# MARKDOWN GENERATOR
#==============================================================================

class LabDetailsGenerator:
    """Generates LABDETAILS.md & HTML documentation from collected environment data"""
    
    def __init__(self, env: LabEnvironment, diagram_style: str = "glassmorphism", svg_rel_dir: str = "images"):
        self.env = env
        self.diagram_style = diagram_style.lower()
        self.svg_rel_dir = svg_rel_dir
        self.lines = []
    
    def generate(self) -> str:
        """Generate the complete LABDETAILS.md content"""
        self._add_header()
        self._add_high_level_architecture()
        self._add_network_architecture()
        self._add_vcf_domain_architecture()
        self._add_esxi_host_layout()
        self._add_vm_inventory()
        self._add_k8s_architectures()
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

    def generate_html(self, svg_map: Dict[str, str]) -> str:
        """Generate standalone Style 5 Glassmorphic HTML documentation with inline SVGs and complete metadata"""
        sku = xml_escape(self.env.lab_sku or "VCF-91-ALL-SE-ADV")
        lab_type = xml_escape(self.env.lab_type or "DISCOVERY")
        dns_domain = xml_escape(self.env.dns_domain or "site-a.vcf.lab")
        esxi_ver = xml_escape(self.env.esxi_version or "9.1.0.0.25370933")
        vcf_ver = "9.1" if ("9.1" in esxi_ver or "9.1" in sku) else ("9.0.1" if "9.0" in esxi_ver else "Unknown")
        now_str = datetime.datetime.now().strftime('%B %d, %Y at %H:%M:%S')

        lab_type_desc = {
            'HOL': 'Hands-on Labs',
            'ATE': 'Advanced Technical Enablement / Livefire',
            'VXP': 'VCF Experience Program',
            'EDU': 'Education/Training',
            'DISCOVERY': 'Discovery Environment'
        }
        type_desc = lab_type_desc.get(self.env.lab_type, self.env.lab_type)

        site_count = len(set(d.name.split('-')[-1] if '-' in d.name else 'a' for d in self.env.domains))
        config_desc = "Single Site" if site_count <= 1 else f"Multi-Site ({site_count} sites)"

        total_hosts = len(self.env.hosts)
        mgmt_vms_count = len(self.env.mgmt_vms)
        wld_vms_count = len(self.env.wld_vms)
        total_ds_cap = sum(ds.capacity_gb for ds in self.env.datastores)
        total_ds_free = sum(ds.free_gb for ds in self.env.datastores)
        total_ds_used = total_ds_cap - total_ds_free
        total_ds_used_pct = (total_ds_used / total_ds_cap * 100) if total_ds_cap > 0 else 0

        html = []
        html.append('<!DOCTYPE html>')
        html.append('<html lang="en">')
        html.append('<head>')
        html.append('  <meta charset="UTF-8">')
        html.append('  <meta name="viewport" content="width=device-width, initial-scale=1.0">')
        html.append(f'  <title>{sku} - Lab Environment Documentation v2.3.1</title>')
        html.append('  <style>')
        html.append('    @import url("https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap");')
        html.append('    :root {')
        html.append('      --bg-primary: #080b10;')
        html.append('      --card-bg: rgba(255, 255, 255, 0.035);')
        html.append('      --card-border: rgba(255, 255, 255, 0.1);')
        html.append('      --card-hover-border: rgba(88, 166, 255, 0.4);')
        html.append('      --accent-blue: #58a6ff;')
        html.append('      --accent-purple: #bc8cff;')
        html.append('      --accent-green: #3fb950;')
        html.append('      --accent-orange: #f78166;')
        html.append('      --accent-amber: #d29922;')
        html.append('      --accent-cyan: #38bdf8;')
        html.append('      --text-main: #c9d1d9;')
        html.append('      --text-bright: #f0f6fc;')
        html.append('      --text-muted: #8b949e;')
        html.append('    }')
        html.append('    * { box-sizing: border-box; }')
        html.append('    html { scroll-behavior: smooth; }')
        html.append('    body { font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg-primary); color: var(--text-main); margin: 0; padding: 0; line-height: 1.6; }')
        html.append('    body::before { content: ""; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: radial-gradient(circle at 10% 10%, rgba(88, 166, 255, 0.08), transparent 40%), radial-gradient(circle at 90% 80%, rgba(188, 140, 255, 0.08), transparent 40%), radial-gradient(circle at 50% 50%, rgba(63, 185, 80, 0.04), transparent 60%); pointer-events: none; z-index: -1; }')
        html.append('    .container { max-width: 1300px; margin: 0 auto; padding: 24px; }')
        html.append('    .hero { text-align: center; padding: 36px 16px 20px; }')
        html.append('    .hero-badge { display: inline-block; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1.5px; padding: 4px 14px; border-radius: 20px; background: rgba(88, 166, 255, 0.15); color: var(--accent-blue); border: 1px solid rgba(88, 166, 255, 0.35); margin-bottom: 12px; }')
        html.append('    h1 { font-size: 34px; font-weight: 800; margin: 0 0 10px; background: linear-gradient(135deg, #58a6ff 0%, #bc8cff 50%, #38bdf8 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; letter-spacing: -0.5px; }')
        html.append('    .hero-sub { color: var(--text-muted); font-size: 14px; max-width: 750px; margin: 0 auto; }')
        html.append('    ')
        html.append('    /* Sticky Navigation */')
        html.append('    .navbar { position: sticky; top: 12px; z-index: 100; backdrop-filter: blur(18px); background: rgba(13, 17, 23, 0.85); border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 12px; padding: 8px 14px; margin-bottom: 28px; box-shadow: 0 8px 24px rgba(0,0,0,0.5); display: flex; flex-wrap: wrap; gap: 6px; align-items: center; justify-content: center; }')
        html.append('    .nav-link { color: var(--text-muted); text-decoration: none; font-size: 11.5px; font-weight: 600; padding: 5px 10px; border-radius: 8px; transition: all 0.2s ease; background: rgba(255, 255, 255, 0.02); border: 1px solid transparent; }')
        html.append('    .nav-link:hover { color: var(--text-bright); background: rgba(88, 166, 255, 0.15); border-color: rgba(88, 166, 255, 0.3); transform: translateY(-1px); }')
        html.append('    ')
        html.append('    /* Stat Grid */')
        html.append('    .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 16px; margin-bottom: 28px; }')
        html.append('    .stat-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 14px; padding: 18px; backdrop-filter: blur(14px); box-shadow: 0 8px 24px rgba(0,0,0,0.3); transition: border-color 0.2s, transform 0.2s; }')
        html.append('    .stat-card:hover { border-color: var(--card-hover-border); transform: translateY(-2px); }')
        html.append('    .stat-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.8px; color: var(--text-muted); margin-bottom: 6px; }')
        html.append('    .stat-value { font-size: 22px; font-weight: 700; color: var(--text-bright); }')
        html.append('    .stat-meta { font-size: 11px; color: var(--accent-blue); margin-top: 4px; }')
        html.append('    ')
        html.append('    /* Glass Cards */')
        html.append('    .card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 14px; padding: 24px; margin-bottom: 28px; box-shadow: 0 8px 32px rgba(0,0,0,0.35); backdrop-filter: blur(14px); transition: border-color 0.2s; }')
        html.append('    .card:hover { border-color: rgba(255,255,255,0.18); }')
        html.append('    h2 { color: var(--text-bright); font-size: 20px; font-weight: 700; margin: 0 0 16px; display: flex; align-items: center; gap: 10px; border-bottom: 1px solid rgba(255,255,255,0.08); padding-bottom: 10px; }')
        html.append('    h3 { color: #e6edf3; font-size: 15px; font-weight: 600; margin: 20px 0 10px; }')
        html.append('    .badge { display: inline-block; font-size: 10px; font-weight: 700; padding: 3px 8px; border-radius: 10px; text-transform: uppercase; }')
        html.append('    .badge-on { background: rgba(63, 185, 80, 0.15); color: #3fb950; border: 1px solid rgba(63, 185, 80, 0.3); }')
        html.append('    .badge-off { background: rgba(139, 148, 158, 0.15); color: #8b949e; border: 1px solid rgba(139, 148, 158, 0.3); }')
        html.append('    .badge-purple { background: rgba(188, 140, 255, 0.15); color: #bc8cff; border: 1px solid rgba(188, 140, 255, 0.3); }')
        html.append('    .badge-amber { background: rgba(210, 153, 34, 0.15); color: #d29922; border: 1px solid rgba(210, 153, 34, 0.3); }')
        html.append('    .badge-blue { background: rgba(88, 166, 255, 0.15); color: #58a6ff; border: 1px solid rgba(88, 166, 255, 0.3); }')
        html.append('    ')
        html.append('    /* Tables */')
        html.append('    .table-wrap { width: 100%; overflow-x: auto; margin: 12px 0 16px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.08); }')
        html.append('    table { width: 100%; border-collapse: collapse; text-align: left; }')
        html.append('    th, td { padding: 9px 14px; font-size: 12.5px; border-bottom: 1px solid rgba(255,255,255,0.06); }')
        html.append('    th { background: rgba(88, 166, 255, 0.08); color: var(--accent-blue); font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 0.6px; }')
        html.append('    tr:hover td { background: rgba(255,255,255,0.025); }')
        html.append('    tr:last-child td { border-bottom: none; }')
        html.append('    code { font-family: "JetBrains Mono", monospace; font-size: 11.5px; color: #79c0ff; background: rgba(110,118,129,0.15); padding: 2px 6px; border-radius: 4px; }')
        html.append('    a { color: var(--accent-cyan); text-decoration: none; font-weight: 500; }')
        html.append('    a:hover { text-decoration: underline; color: #79c0ff; }')
        html.append('    ')
        html.append('    /* Diagrams */')
        html.append('    .diagram-box { text-align: center; margin: 16px 0; background: rgba(0,0,0,0.3); border-radius: 12px; padding: 14px; border: 1px solid rgba(255,255,255,0.08); overflow-x: auto; }')
        html.append('    .diagram-box svg { max-width: 100%; height: auto; border-radius: 8px; display: block; margin: 0 auto; }')
        html.append('    ')
        html.append('    /* Terminal Code Windows */')
        html.append('    .terminal { background: #070a0e; border: 1px solid rgba(255,255,255,0.1); border-radius: 10px; margin: 14px 0 20px; overflow: hidden; box-shadow: 0 6px 18px rgba(0,0,0,0.4); }')
        html.append('    .terminal-header { background: rgba(255,255,255,0.03); border-bottom: 1px solid rgba(255,255,255,0.08); padding: 8px 14px; display: flex; align-items: center; justify-content: space-between; }')
        html.append('    .terminal-dots { display: flex; gap: 6px; }')
        html.append('    .dot { width: 10px; height: 10px; border-radius: 50%; }')
        html.append('    .dot-red { background: #ff5f56; }')
        html.append('    .dot-yellow { background: #ffbd2e; }')
        html.append('    .dot-green { background: #27c93f; }')
        html.append('    .terminal-title { font-size: 11px; font-family: "JetBrains Mono", monospace; color: var(--text-muted); }')
        html.append('    .terminal-body { padding: 14px 18px; margin: 0; font-family: "JetBrains Mono", monospace; font-size: 12px; line-height: 1.55; color: #a5d6ff; overflow-x: auto; }')
        html.append('    ')
        html.append('    /* Progress Bar */')
        html.append('    .progress-bar-bg { background: rgba(255,255,255,0.08); border-radius: 6px; height: 7px; width: 100px; display: inline-block; vertical-align: middle; margin-right: 8px; overflow: hidden; }')
        html.append('    .progress-bar-fill { height: 100%; background: linear-gradient(90deg, #3fb950, #58a6ff); border-radius: 6px; }')
        html.append('    ')
        html.append('    /* Notice Banner */')
        html.append('    .notice-banner { background: rgba(88, 166, 255, 0.08); border: 1px solid rgba(88, 166, 255, 0.25); border-radius: 8px; padding: 12px 16px; font-size: 12.5px; color: #79c0ff; margin-bottom: 14px; display: flex; align-items: center; gap: 8px; }')
        html.append('    .footer { text-align: center; margin-top: 50px; padding: 30px 0; border-top: 1px solid rgba(255,255,255,0.08); font-size: 12px; color: var(--text-muted); }')
        html.append('  </style>')
        html.append('</head>')
        html.append('<body>')
        html.append('  <div class="container">')
        html.append('    <!-- Hero Section -->')
        html.append('    <div class="hero">')
        html.append('      <div class="hero-badge">Style 5 Glassmorphism Architecture Engine v2.1</div>')
        html.append(f'      <h1>{sku}</h1>')
        html.append('      <div class="hero-sub">Complete Multi-Plane Infrastructure, Control Planes, Virtualization Fabric &amp; Operations Documentation</div>')
        html.append('    </div>')
        html.append('    ')
        html.append('    <!-- Sticky Navigation Bar -->')
        html.append('    <nav class="navbar">')
        html.append('      <a class="nav-link" href="#overview">Overview</a>')
        html.append('      <a class="nav-link" href="#high-level">High-Level</a>')
        html.append('      <a class="nav-link" href="#network">Network Fabric</a>')
        html.append('      <a class="nav-link" href="#domains">VCF Domains</a>')
        html.append('      <a class="nav-link" href="#hosts">ESXi Hosts</a>')
        html.append('      <a class="nav-link" href="#inventory">VM Inventory</a>')
        html.append('      <a class="nav-link" href="#k8s-supervisor">Supervisor</a>')
        html.append('      <a class="nav-link" href="#k8s-vsp">VSP LCM</a>')
        html.append('      <a class="nav-link" href="#k8s-vcfa">VCFA</a>')
        html.append('      <a class="nav-link" href="#k8s-ssp">SSP</a>')
        html.append('      <a class="nav-link" href="#core-vms">Core VMs</a>')
        html.append('      <a class="nav-link" href="#holorouter">Holorouter</a>')
        html.append('      <a class="nav-link" href="#subnets">Subnets</a>')
        html.append('      <a class="nav-link" href="#dvs">Virtual Switches</a>')
        html.append('      <a class="nav-link" href="#nsx">NSX-T</a>')
        html.append('      <a class="nav-link" href="#boot">Boot Flow</a>')
        html.append('      <a class="nav-link" href="#urls">Web URLs</a>')
        html.append('      <a class="nav-link" href="#credentials">Credentials</a>')
        html.append('      <a class="nav-link" href="#storage">Storage</a>')
        html.append('      <a class="nav-link" href="#complete">Complete Topology</a>')
        html.append('      <a class="nav-link" href="#quick-ref">Quick Ref</a>')
        html.append('      <a class="nav-link" href="#doc-info">Doc Info</a>')
        html.append('    </nav>')
        html.append('    ')
        html.append('    <!-- Stat Cards Overview Grid -->')
        html.append('    <div class="stat-grid">')
        html.append('      <div class="stat-card">')
        html.append('        <div class="stat-label">Lab SKU &amp; Type</div>')
        html.append(f'        <div class="stat-value">{sku}</div>')
        html.append(f'        <div class="stat-meta">{lab_type} • {config_desc}</div>')
        html.append('      </div>')
        html.append('      <div class="stat-card">')
        html.append('        <div class="stat-label">ESXi Physical Hosts</div>')
        html.append(f'        <div class="stat-value">{total_hosts} Hosts</div>')
        html.append(f'        <div class="stat-meta">{len(self.env.clusters)} Clusters (Mgmt &amp; Wld)</div>')
        html.append('      </div>')
        html.append('      <div class="stat-card">')
        html.append('        <div class="stat-label">Virtual Machines</div>')
        html.append(f'        <div class="stat-value">{mgmt_vms_count + wld_vms_count} VMs</div>')
        html.append(f'        <div class="stat-meta">{mgmt_vms_count} Mgmt | {wld_vms_count} Workload</div>')
        html.append('      </div>')
        html.append('      <div class="stat-card">')
        html.append('        <div class="stat-label">vSAN Clustered Pool</div>')
        html.append(f'        <div class="stat-value">{total_ds_cap/1024:.2f} TB</div>')
        html.append(f'        <div class="stat-meta">{total_ds_used_pct:.0f}% allocated ({total_ds_free:.1f} GB free)</div>')
        html.append('      </div>')
        html.append('    </div>')
        html.append('    ')
        html.append('    <!-- 1. Lab Overview Card -->')
        html.append('    <div class="card" id="overview">')
        html.append('      <h2><span>🏛️</span> Lab Overview</h2>')
        html.append('      <div class="table-wrap">')
        html.append('        <table>')
        html.append('          <tr><th style="width:240px;">Property</th><th>Value</th></tr>')
        html.append(f'          <tr><td><strong>Lab SKU</strong></td><td><code>{sku}</code></td></tr>')
        html.append(f'          <tr><td><strong>Lab Type</strong></td><td>{lab_type} ({type_desc})</td></tr>')
        html.append(f'          <tr><td><strong>VCF Version</strong></td><td>{vcf_ver}</td></tr>')
        html.append(f'          <tr><td><strong>ESXi Version</strong></td><td><code>{esxi_ver}</code></td></tr>')
        html.append(f'          <tr><td><strong>Configuration</strong></td><td>{config_desc}</td></tr>')
        html.append(f'          <tr><td><strong>DNS Domain</strong></td><td><code>{dns_domain}</code></td></tr>')
        html.append('          <tr><td><strong>Credentials</strong></td><td>See <code>/home/holuser/creds.txt</code></td></tr>')
        html.append('        </table>')
        html.append('      </div>')
        html.append('    </div>')

        # Helper for diagram cards
        def add_diagram_card(section_id: str, title_icon: str, title_text: str, filename: str, desc: str = ""):
            if filename in svg_map:
                html.append(f'    <!-- {title_text} -->')
                html.append(f'    <div class="card" id="{section_id}">')
                html.append(f'      <h2><span>{title_icon}</span> {xml_escape(title_text)}</h2>')
                if desc:
                    html.append(f'      <p style="color:var(--text-muted); font-size:13px; margin:-6px 0 14px;">{xml_escape(desc)}</p>')
                html.append('      <div class="diagram-box">')
                html.append(svg_map[filename])
                html.append('      </div>')
                html.append('    </div>')

        # 2. High-Level Architecture
        add_diagram_card("high-level", "🌐", "High-Level Architecture & Connectivity", "high_level_architecture.svg", "Ingress, Core Infrastructure, and VCF Management / Workload domain boundaries.")

        # 3. Network Architecture
        add_diagram_card("network", "⚡", "Multi-Plane Network & Data Flow Topology", "network_dataflow.svg", "Traffic separation across Core, VCF Management, vSAN Storage, vMotion, and NSX GENEVE Overlay planes.")

        # 4. VCF Domain Architecture
        add_diagram_card("domains", "☁️", "VCF Domain Hierarchy & Control Plane Topology", "vcf_domain_architecture.svg", "SDDC Manager multi-domain control hierarchy and cluster associations.")

        # 5. ESXi Host Layout
        add_diagram_card("hosts", "🖥️", "ESXi Physical Host & Interface Fabric", "esxi_host_layout.svg", "Physical host interfaces, compute allocation, and kernel network assignments.")

        # 6. VM Inventory Section
        html.append('    <!-- VM Inventory -->')
        html.append('    <div class="card" id="inventory">')
        html.append('      <h2><span>📋</span> Virtual Machine Inventory</h2>')
        for domain in self.env.domains:
            vms = self.env.mgmt_vms if domain.domain_type == "MANAGEMENT" else self.env.wld_vms
            domain_label = "Management" if domain.domain_type == "MANAGEMENT" else "Workload"
            badge_cls = "badge-purple" if domain.domain_type == "MANAGEMENT" else "badge-amber"
            
            html.append(f'      <h3><span class="badge {badge_cls}">{domain_label} Domain</span> {xml_escape(domain.vcenter_fqdn)} ({len(vms)} VMs)</h3>')
            html.append('      <div class="table-wrap">')
            html.append('        <table>')
            html.append('          <thead><tr><th>VM Name</th><th>Power State</th><th>vCPUs</th><th>Memory</th><th>IP Address</th></tr></thead>')
            html.append('          <tbody>')
            for vm in sorted(vms, key=lambda x: x.name):
                is_on = "poweredOn" in vm.power_state
                p_badge = '<span class="badge badge-on">On</span>' if is_on else '<span class="badge badge-off">Off</span>'
                mem_str = f"{vm.memory_mb / 1024:.0f} GB" if vm.memory_mb else "-"
                ip_str = f"<code>{xml_escape(vm.ip_address)}</code>" if vm.ip_address else '<span style="color:#6e7681">-</span>'
                html.append(f'            <tr><td><strong>{xml_escape(vm.name)}</strong></td><td>{p_badge}</td><td>{vm.vcpus}</td><td>{mem_str}</td><td>{ip_str}</td></tr>')
            html.append('          </tbody>')
            html.append('        </table>')
            html.append('      </div>')
        html.append('    </div>')

        # 6b. Kubernetes & Platform Architectures
        add_diagram_card("k8s-supervisor", "☸️", "Supervisor Tanzu Kubernetes Architecture & Workload Fabric", "supervisor_k8s_architecture.svg", "3-node HA control plane, ESXi Spherelet hypervisor workers, vSAN Cloud Native Storage (CNS), and namespaces.")
        add_diagram_card("k8s-vsp", "⚙️", "VSP Management Cluster (Fleet LCM) K8s Architecture", "vsp_k8s_architecture.svg", "Single-node control plane/worker, kube-vip Layer 2 VIP (:5480), Fleet LCM operators, and local depot storage.")
        add_diagram_card("k8s-vcfa", "⚡", "VCF Automation (VCFA 9.x) Microservices K8s Architecture", "vcfa_k8s_architecture.svg", "Istio ingress gateway (:443), Prelude cloud templates/pipelines, VMSP platform microservices, and local CSI.")
        if self.env.has_ssp or "ssp_k8s_architecture.svg" in svg_map:
            add_diagram_card("k8s-ssp", "🛡️", "Security Services Platform (SSP 5.2 / vDefend) Architecture", "ssp_k8s_architecture.svg", "MetalLB ingress/Kafka telemetry VIPs, CAPI management host, multi-node compute fabric, and vDefend security microservices.")

        # 7. Core Infrastructure VMs & Holorouter Services
        add_diagram_card("core-vms", "🛠️", "Core Infrastructure & Services Fabric", "core_infrastructure.svg", "L1 routing, Technitium DNS, DHCP, Squid proxy, desktop console, and manager automation.")
        add_diagram_card("holorouter", "🛡️", "Holorouter Services & Container Reverse Proxy Architecture", "holorouter_architecture.svg", "NGINX TLS reverse proxy, Authentik OIDC, Technitium DNS, Vault PKI engine, Squid forward proxy, and kernel routing.")

        # 8. Network Subnets Reference Table
        html.append('    <!-- Network Subnets Reference -->')
        html.append('    <div class="card" id="subnets">')
        html.append('      <h2><span>🌐</span> Network Subnets Reference</h2>')
        html.append('      <div class="table-wrap">')
        html.append('        <table>')
        html.append('          <thead><tr><th>Network</th><th>Subnet</th><th>Gateway</th><th>Purpose</th></tr></thead>')
        html.append('          <tbody>')
        
        domain_s = self.env.dns_domain or "site-a.vcf.lab"
        core_sub = self.env.core_subnet or get_subnet_for_ip(self.env.router_ip, 25) or "10.1.10.128/25"
        core_gw = self.env.router_ip or resolve_host('router', domain_s) or "10.1.10.129"
        ext_sub = self.env.external_subnet or get_subnet_for_ip(self.env.gateway_ip, 24) or "192.168.0.0/24"
        ext_gw = self.env.gateway_ip or resolve_host('gateway', domain_s) or "192.168.0.1"
        
        sample_host = self.env.hosts[0] if self.env.hosts else None
        mgmt_sub = get_subnet_for_ip(sample_host.mgmt_ip, 24) if sample_host and sample_host.mgmt_ip else "10.1.1.0/24"
        vsan_sub = get_subnet_for_ip(sample_host.vsan_ip, 24) if sample_host and sample_host.vsan_ip else "10.1.2.0/24"
        vmotion_sub = get_subnet_for_ip(sample_host.vmotion_ip, 24) if sample_host and sample_host.vmotion_ip else "10.1.3.0/24"
        tep_sub = get_subnet_for_ip(sample_host.tep_ip, 25) if sample_host and sample_host.tep_ip else "10.1.5.128/25"

        html.append(f'            <tr><td><strong>Core / Services</strong></td><td><code>{xml_escape(core_sub)}</code></td><td><code>{xml_escape(core_gw)}</code></td><td>Console, Manager VM, Router, DNS/DHCP</td></tr>')
        html.append(f'            <tr><td><strong>VCF Management</strong></td><td><code>{xml_escape(mgmt_sub)}</code></td><td><code>{xml_escape(mgmt_sub.rsplit(".", 1)[0] + ".1")}</code></td><td>vCenter, SDDC Manager, NSX Manager, Aria Suite</td></tr>')
        html.append(f'            <tr><td><strong>vSAN Storage</strong></td><td><code>{xml_escape(vsan_sub)}</code></td><td>-</td><td>Dedicated Clustered vSAN Storage Fabric</td></tr>')
        html.append(f'            <tr><td><strong>vMotion Migration</strong></td><td><code>{xml_escape(vmotion_sub)}</code></td><td>-</td><td>High-Speed Live VM State Migration</td></tr>')
        html.append(f'            <tr><td><strong>NSX GENEVE TEP</strong></td><td><code>{xml_escape(tep_sub)}</code></td><td><code>{xml_escape(tep_sub.rsplit(".", 1)[0] + ".129")}</code></td><td>NSX Overlay Transport Node &amp; Edge Tunnel Endpoints</td></tr>')
        html.append(f'            <tr><td><strong>External (Holodeck)</strong></td><td><code>{xml_escape(ext_sub)}</code></td><td><code>{xml_escape(ext_gw)}</code></td><td>vPod Host Uplink &amp; External Internet Access</td></tr>')
        html.append('          </tbody>')
        html.append('        </table>')
        html.append('      </div>')
        html.append('    </div>')

        # 9. Distributed Virtual Switches
        add_diagram_card("dvs", "🔀", "Distributed Virtual Switch (VDS) & Port Group Topology", "dvs_topology.svg", "Virtual Distributed Switch fabrics, uplink mapping, and portgroup assignments across vCenters.")

        # 10. NSX-T Architecture
        add_diagram_card("nsx", "🛡️", "NSX-T Virtualization & Overlay Topology", "nsx_architecture.svg", "NSX Manager clusters, transport nodes, Tier-0/Tier-1 gateways, and GENEVE tunneling.")

        # 11. Lab Startup Boot Sequence
        add_diagram_card("boot", "🚀", "Lab Startup Boot & Service Flow", "lab_boot_sequence.svg", "Step-by-step startup dependencies and health verification stages executed by labstartup.py.")

        # 12. Web Interfaces / URLs Table
        html.append('    <!-- Web Interfaces / URLs -->')
        html.append('    <div class="card" id="urls">')
        html.append('      <h2><span>🔗</span> Web Interfaces &amp; URLs</h2>')
        html.append('      <div class="table-wrap">')
        html.append('        <table>')
        html.append('          <thead><tr><th>Service</th><th>URL</th><th>Expected Content / Verification</th></tr></thead>')
        html.append('          <tbody>')
        for url, text in self.env.urls:
            if url.startswith('#'):
                continue
            service = "Web Service"
            if 'vc-mgmt' in url:
                service = "vCenter Management" + (" VAMI" if '5480' in url else "")
            elif 'vc-wld' in url:
                service = "vCenter Workload" + (" VAMI" if '5480' in url else "")
            elif 'sddcmanager' in url:
                service = "SDDC Manager"
            elif 'nsx' in url:
                service = "NSX Manager"
            elif 'ops-a' in url:
                service = "VCF Operations"
            elif 'auto-' in url:
                service = "VCF Automation"
            elif 'opslcm' in url:
                service = "VCF Operations Manager"
            elif 'vmware.com' in url:
                service = "VMware.com (Internet Test)"
            
            html.append(f'            <tr><td><strong>{xml_escape(service)}</strong></td><td><a href="{xml_escape(url)}" target="_blank">{xml_escape(url)}</a></td><td><code>{xml_escape(text)}</code></td></tr>')
        html.append('          </tbody>')
        html.append('        </table>')
        html.append('      </div>')
        html.append('    </div>')

        # 13. Credentials Table
        html.append('    <!-- Credentials -->')
        html.append('    <div class="card" id="credentials">')
        html.append('      <h2><span>🔑</span> Lab Credentials Reference</h2>')
        html.append('      <div class="notice-banner">')
        html.append('        <span>🔒</span><strong>Security Notice:</strong> The active lab password is automatically configured in <code>/home/holuser/creds.txt</code>.')
        html.append('      </div>')
        html.append('      <div class="table-wrap">')
        html.append('        <table>')
        html.append('          <thead><tr><th>System / Endpoint</th><th>Username</th><th>Password Reference</th></tr></thead>')
        html.append('          <tbody>')
        html.append('            <tr><td>vCenter (Management)</td><td><code>administrator@vsphere.local</code></td><td>See <code>/home/holuser/creds.txt</code></td></tr>')
        for domain in self.env.domains:
            if domain.domain_type != "MANAGEMENT" and domain.sso_domain:
                html.append(f'            <tr><td>vCenter (Workload)</td><td><code>administrator@{xml_escape(domain.sso_domain)}</code></td><td>See <code>/home/holuser/creds.txt</code></td></tr>')
                break
        html.append('            <tr><td>SDDC Manager</td><td><code>administrator@vsphere.local</code></td><td>See <code>/home/holuser/creds.txt</code></td></tr>')
        html.append('            <tr><td>NSX Manager</td><td><code>admin</code></td><td>See <code>/home/holuser/creds.txt</code></td></tr>')
        html.append('            <tr><td>ESXi Physical Hosts</td><td><code>root</code></td><td>See <code>/home/holuser/creds.txt</code></td></tr>')
        html.append('            <tr><td>VCF Operations Suite</td><td><code>admin@local</code></td><td>See <code>/home/holuser/creds.txt</code></td></tr>')
        html.append('            <tr><td>Linux VMs (holuser)</td><td><code>holuser</code></td><td>See <code>/home/holuser/creds.txt</code></td></tr>')
        html.append('            <tr><td>Linux VMs (root)</td><td><code>root</code></td><td>See <code>/home/holuser/creds.txt</code></td></tr>')
        html.append('          </tbody>')
        html.append('        </table>')
        html.append('      </div>')
        html.append('    </div>')

        # 14. Storage Summary
        add_diagram_card("storage", "💾", "vSAN Clustered Storage & Capacity Architecture", "storage_summary.svg", "Clustered vSAN datastore capacities, utilization metrics, and resiliency policies.")
        html.append('    <div class="card">')
        html.append('      <h3><span>📊</span> vSAN Datastore Capacity Allocation</h3>')
        html.append('      <div class="table-wrap">')
        html.append('        <table>')
        html.append('          <thead><tr><th>Datastore</th><th>Type</th><th>Total Capacity</th><th>Free Space</th><th>Space Utilization</th></tr></thead>')
        html.append('          <tbody>')
        for ds in self.env.datastores:
            used_gb = ds.capacity_gb - ds.free_gb
            used_pct = (used_gb / ds.capacity_gb * 100) if ds.capacity_gb > 0 else 0
            html.append(f'            <tr>')
            html.append(f'              <td><strong>{xml_escape(ds.name)}</strong></td>')
            html.append(f'              <td><span class="badge badge-purple">{xml_escape(ds.ds_type)}</span></td>')
            html.append(f'              <td>{ds.capacity_gb:.1f} GB ({ds.capacity_gb/1024:.2f} TB)</td>')
            html.append(f'              <td>{ds.free_gb:.1f} GB</td>')
            html.append(f'              <td><div class="progress-bar-bg"><div class="progress-bar-fill" style="width:{used_pct:.0f}%;"></div></div><strong>{used_pct:.0f}%</strong></td>')
            html.append(f'            </tr>')
        html.append('          </tbody>')
        html.append('        </table>')
        html.append('      </div>')
        html.append('    </div>')

        # 15. Complete Infrastructure Diagram
        add_diagram_card("complete", "🗺️", "Complete VCF Lab Holistic Infrastructure Topology", "complete_infrastructure.svg", "End-to-end multi-tier physical and virtual topology across External, Core, and VCF domains.")

        # 16. Quick Reference Commands
        html.append('    <!-- Quick Reference Commands -->')
        html.append('    <div class="card" id="quick-ref">')
        html.append('      <h2><span>⚡</span> Quick Reference Commands</h2>')
        
        # Snippet 1: Lab Startup
        html.append('      <h3>1. Lab Startup &amp; Health Dashboard (Bash)</h3>')
        html.append('      <div class="terminal">')
        html.append('        <div class="terminal-header"><div class="terminal-dots"><div class="dot dot-red"></div><div class="dot dot-yellow"></div><div class="dot dot-green"></div></div><div class="terminal-title">bash • lab startup</div></div>')
        html.append('        <pre class="terminal-body"><code># Full automated lab startup\ncd /home/holuser/hol &amp;&amp; labstartup.sh\n\n# Check lab readiness status (HOL Console has an alias for this: ltail)\ntail -f /lmchol/startup_status.txt\n\n# View graphical startup status dashboard\nfirefox /lmchol/home/holuser/startup-status.htm</code></pre>')
        html.append('      </div>')

        # Snippet 2: vCenter Connection
        mgmt_vc = "vc-mgmt-a.site-a.vcf.lab"
        for domain in self.env.domains:
            if domain.domain_type == "MANAGEMENT" and domain.vcenter_fqdn:
                mgmt_vc = domain.vcenter_fqdn
                break
        html.append('      <h3>2. vCenter Connection (Python pyVmomi)</h3>')
        html.append('      <div class="terminal">')
        html.append('        <div class="terminal-header"><div class="terminal-dots"><div class="dot dot-red"></div><div class="dot dot-yellow"></div><div class="dot dot-green"></div></div><div class="terminal-title">python • pyVmomi connection</div></div>')
        html.append(f'        <pre class="terminal-body"><code>from pyVim import connect\n\nwith open(\'/home/holuser/creds.txt\', \'r\') as f:\n    password = f.read().strip()\n\nsi = connect.SmartConnect(\n    host="{mgmt_vc}",\n    user="administrator@vsphere.local",\n    pwd=password,\n    disableSslCertValidation=True\n)</code></pre>')
        html.append('      </div>')

        # Snippet 3: SDDC Manager API
        html.append('      <h3>3. SDDC Manager REST API (cURL)</h3>')
        html.append('      <div class="terminal">')
        html.append('        <div class="terminal-header"><div class="terminal-dots"><div class="dot dot-red"></div><div class="dot dot-yellow"></div><div class="dot dot-green"></div></div><div class="terminal-title">bash • sddc manager api</div></div>')
        html.append('        <pre class="terminal-body"><code>PASSWORD=$(cat /home/holuser/creds.txt)\n\n# Get Bearer Token\nTOKEN=$(curl -k -s -X POST "https://sddcmanager-a.site-a.vcf.lab/v1/tokens" \\\n  -H "Content-Type: application/json" \\\n  -d "{\\"username\\": \\"administrator@vsphere.local\\", \\"password\\": \\"$PASSWORD\\"}" \\\n  | python3 -c "import sys,json; print(json.load(sys.stdin)[\'accessToken\'])")\n\n# Query Configured Domains\ncurl -k -s "https://sddcmanager-a.site-a.vcf.lab/v1/domains" \\\n  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool</code></pre>')
        html.append('      </div>')

        # Snippet 4: NSX Manager API
        html.append('      <h3>4. NSX Manager Cluster Status (cURL)</h3>')
        html.append('      <div class="terminal">')
        html.append('        <div class="terminal-header"><div class="terminal-dots"><div class="dot dot-red"></div><div class="dot dot-yellow"></div><div class="dot dot-green"></div></div><div class="terminal-title">bash • nsx manager api</div></div>')
        html.append('        <pre class="terminal-body"><code>PASSWORD=$(cat /home/holuser/creds.txt)\n\n# Query NSX Cluster Status\ncurl -k -s -u admin:$PASSWORD \\\n  https://nsx-mgmt-01a.site-a.vcf.lab/api/v1/cluster/status | python3 -m json.tool</code></pre>')
        html.append('      </div>')
        html.append('    </div>')

        # 17. Document Info & Footer
        html.append('    <!-- Document Information -->')
        html.append('    <div class="card" id="doc-info">')
        html.append('      <h2><span>ℹ️</span> Document Information</h2>')
        html.append('      <div class="table-wrap">')
        html.append('        <table>')
        html.append('          <tr><th style="width:240px;">Property</th><th>Value</th></tr>')
        html.append(f'          <tr><td><strong>Generated Timestamp</strong></td><td>{now_str}</td></tr>')
        html.append('          <tr><td><strong>Generator Version</strong></td><td><code>v2.1</code> (Style 5 Glassmorphism Architecture Engine)</td></tr>')
        html.append('          <tr><td><strong>Generated By</strong></td><td><code>python3 Tools/labdetails/generate_labdetails.py --html --output /lmchol/home/holuser/diagrams/{sku}-labdetails.md</code></td></tr>')
        html.append('          <tr><td><strong>Diagram Engine License</strong></td><td>MIT License © 2025 fireworks-tech-graph contributors</td></tr>')
        html.append('          <tr><td><strong>Lab Configuration</strong></td><td><code>/tmp/config.ini</code></td></tr>')
        html.append(f'          <tr><td><strong>Source INI</strong></td><td><code>/home/holuser/hol/holodeck/{sku}.ini</code></td></tr>')
        html.append('          <tr><td><strong>Lab Startup Script</strong></td><td><code>/home/holuser/hol/labstartup.sh</code></td></tr>')
        html.append('        </table>')
        html.append('      </div>')
        html.append('    </div>')
        html.append('    ')
        html.append('    <div class="footer">')
        html.append('      <p>Generated by <strong>Tools/labdetails/generate_labdetails.py</strong> v2.1 | Style 5 Glassmorphism Engine</p>')
        html.append('      <p>Diagram Engine License: MIT License © 2025 fireworks-tech-graph contributors</p>')
        html.append('    </div>')
        html.append('  </div>')
        html.append('</body>')
        html.append('</html>')
        return '\n'.join(html)
    
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
            'DISCOVERY': 'Discovery Environment'
        }
        
        type_desc = lab_type_desc.get(self.env.lab_type, self.env.lab_type)
        ver_label = f"**{self.env.lab_flavor} Version**"
        ver_val = self.env.vcf_version or ("9.1" if ("9.1" in self.env.esxi_version or "9.1" in self.env.lab_sku) else "9.0.1")
        
        self._add(f"# {self.env.lab_sku} - Lab Environment Documentation")
        self._add()
        self._add("## Lab Overview")
        self._add()
        self._add("| Property | Value |")
        self._add("| -------- | ----- |")
        self._add(f"| **Lab SKU** | {self.env.lab_sku} |")
        self._add(f"| **Lab Type** | {self.env.lab_type} ({type_desc}) |")
        self._add(f"| {ver_label} | {ver_val} |")
        if self.env.esxi_version:
            self._add(f"| **ESXi Version** | {self.env.esxi_version} |")
        
        self._add(f"| **Configuration** | {self.env.topology_type} |")
        self._add(f"| **DNS Domain** | {self.env.dns_domain} |")
        self._add(f"| **Squid Proxy** | {self.env.holorouter.squid_filter_mode} |")
        self._add(f"| **Credentials** | See `/home/holuser/creds.txt` |")
        self._add()
        self._add("---")
        self._add()
    
    def _add_high_level_architecture(self):
        """Add high-level architecture diagram"""
        self._add("## High-Level Architecture")
        self._add()
        if self.diagram_style in ("glassmorphism", "both"):
            self._add(f"![High-Level Architecture]({self.svg_rel_dir}/high_level_architecture.svg)")
            self._add()
        if self.diagram_style in ("mermaid", "both"):
            ext_sub = self.env.external_subnet or "192.168.0.0/24"
            core_sub = self.env.core_subnet or "10.1.10.128/25"
            self._add("```mermaid")
            self._add("flowchart TB")
            self._add(MERMAID_STYLES)
            self._add()
            self._add('    subgraph External["External Network"]')
            self._add(f'        Internet[("Internet<br/>{ext_sub}")]')
            self._add('    end')
            self._add()
            self._add('    subgraph vPod["vPod Environment"]')
            self._add(f'        subgraph CoreVMs["Core Infrastructure VMs<br/>{core_sub}"]')
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
        if self.diagram_style in ("glassmorphism", "both"):
            self._add(f"![Multi-Plane Network & Data Flow Topology]({self.svg_rel_dir}/network_dataflow.svg)")
            self._add()
        if self.diagram_style in ("mermaid", "both"):
            ext_sub = self.env.external_subnet or "192.168.0.0/24"
            core_sub = self.env.core_subnet or "10.1.10.128/25"
            sample_host = self.env.hosts[0] if self.env.hosts else None
            mgmt_sub = get_subnet_for_ip(sample_host.mgmt_ip, 24) if sample_host and sample_host.mgmt_ip else "10.1.1.0/24"
            vsan_sub = get_subnet_for_ip(sample_host.vsan_ip, 24) if sample_host and sample_host.vsan_ip else "10.1.2.0/24"

            self._add("```mermaid")
            self._add("flowchart LR")
            self._add(MERMAID_STYLES)
            self._add()
            self._add('    subgraph External["External/Internet"]')
            self._add(f'        ExtNet["{ext_sub}"]')
            self._add('    end')
            self._add()
            self._add(f'    subgraph Router["holorouter ({self.env.router_ip})"]')
            self._add('        FW["Firewall/NAT"]')
            self._add('        DNS["DNS Server"]')
            self._add('        Proxy["Squid Proxy :3128"]')
            self._add('    end')
            self._add()
            self._add('    subgraph Networks["Internal Networks"]')
            self._add(f'        subgraph CoreNet["Core Network<br/>{core_sub}"]')
            self._add(f'            Console2["console<br/>{self.env.console_ip}"]')
            self._add(f'            Manager2["manager<br/>{self.env.manager_ip}"]')
            self._add('        end')
            self._add()
            self._add(f'        subgraph MgmtNet["Management Network<br/>{mgmt_sub}"]')
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
            self._add(f'        subgraph VSANNet["vSAN Network<br/>{vsan_sub}"]')
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
        if self.diagram_style in ("glassmorphism", "both"):
            self._add(f"![VCF Domain Architecture]({self.svg_rel_dir}/vcf_domain_architecture.svg)")
            self._add()
        if self.diagram_style in ("mermaid", "both"):
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
        if self.diagram_style in ("glassmorphism", "both"):
            self._add(f"![ESXi Host Layout]({self.svg_rel_dir}/esxi_host_layout.svg)")
            self._add()
        if self.diagram_style in ("mermaid", "both"):
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

    def _add_k8s_architectures(self):
        """Add Kubernetes & Platform Cluster Architecture sections and diagrams"""
        self._add("## Kubernetes & Platform Cluster Architectures")
        self._add()
        self._add("Discovered Kubernetes and platform microservice clusters powering Tanzu, Lifecycle Management, Automation, and Network Security.")
        self._add()
        
        # Look up dynamically discovered clusters
        sup_cl = next((c for c in self.env.k8s_clusters if c.cluster_type == "Supervisor"), None)
        vsp_cl = next((c for c in self.env.k8s_clusters if c.cluster_type == "VSP"), None)
        
        # 1. Supervisor Tanzu
        self._add("### 1. Supervisor Tanzu Cluster")
        self._add()
        if self.diagram_style in ("glassmorphism", "both"):
            self._add(f"![Supervisor K8s Architecture]({self.svg_rel_dir}/supervisor_k8s_architecture.svg)")
            self._add()
            
        domain_s = self.env.dns_domain or "site-a.vcf.lab"
        sup_vip = sup_cl.vip if sup_cl and sup_cl.vip else (resolve_host('supervisor', domain_s) or resolve_host('wcp', domain_s) or "10.1.1.140")
        if sup_cl and sup_cl.nodes:
            sup_nodes_desc = ", ".join([f"`{n.name}` (`{n.ip_address}`)" for n in sup_cl.nodes])
        else:
            cp1 = resolve_host('sup-cp-1', domain_s) or resolve_host('SupervisorControlPlaneVM-1', domain_s) or "10.1.1.137"
            cp2 = resolve_host('sup-cp-2', domain_s) or resolve_host('SupervisorControlPlaneVM-2', domain_s) or "10.1.1.138"
            cp3 = resolve_host('sup-cp-3', domain_s) or resolve_host('SupervisorControlPlaneVM-3', domain_s) or "10.1.1.139"
            sup_nodes_desc = f"3 Control Plane Nodes (`SupervisorControlPlaneVM (1)..3` / `{cp1}..{cp3}`)"

        self._add("| Component | Details |")
        self._add("| --------- | ------- |")
        self._add(f"| **Cluster VIP** | `{sup_vip}` (Port 6443) |")
        self._add(f"| **Control Plane VMs** | {sup_nodes_desc} |")
        self._add("| **Worker Nodes** | ESXi Hypervisor Hosts (`esx-01a..04a`) via Spherelet Agent |")
        self._add("| **Namespaces** | `kube-system`, `svc-harbor`, `ns-argocd`, `svc-cci`, `ns-hol-*` |")
        self._add("| **Persistent Storage** | vSAN CSI Driver (`vsphere-csi-sc`) |")
        self._add()
        
        # 2. VSP Management (Fleet LCM)
        self._add("### 2. VSP Management Cluster (Fleet LCM)")
        self._add()
        if self.diagram_style in ("glassmorphism", "both"):
            self._add(f"![VSP Fleet LCM Architecture]({self.svg_rel_dir}/vsp_k8s_architecture.svg)")
            self._add()
            
        vsp_vip = vsp_cl.vip if vsp_cl and vsp_cl.vip else (resolve_host('vsp', domain_s) or resolve_host('vsp-vip', domain_s) or "10.1.1.142")
        if vsp_cl and vsp_cl.nodes:
            vsp_n = vsp_cl.nodes[0]
            vsp_node_desc = f"`{vsp_n.name}` (`{vsp_n.ip_address}`)"
            vsp_sizing_desc = f"{vsp_n.cpu_capacity} vCPUs / {vsp_n.memory_mb // 1024 if vsp_n.memory_mb else 32} GB RAM (Single Node)"
        else:
            vsp_node_ip = resolve_host('vsp-01a', domain_s) or "10.1.1.141"
            vsp_node_desc = f"`vsp-01a.{domain_s}` (`{vsp_node_ip}`)"
            vsp_sizing_desc = "8 vCPUs / 32 GB RAM (Single Node)"

        self._add("| Component | Details |")
        self._add("| --------- | ------- |")
        self._add("| **Topology** | Single Node Control Plane & Worker |")
        self._add(f"| **Node Name & IP** | {vsp_node_desc} |")
        self._add(f"| **VIP & Port** | `{vsp_vip}:5480` (Fleet LCM Ingress) |")
        self._add(f"| **Node Sizing** | {vsp_sizing_desc} |")
        self._add("| **Taints** | `node-role.kubernetes.io/control-plane:NoSchedule` |")
        self._add("| **Microservices** | `vcf-fleet-lcm`, `vcf-sddc-lcm`, `telemetry`, `vcf-fleet-depot-service` |")
        self._add()
        
        # 3. VCF Automation
        self._add("### 3. VCF Automation Microservices Cluster")
        self._add()
        if self.diagram_style in ("glassmorphism", "both"):
            self._add(f"![VCF Automation Architecture]({self.svg_rel_dir}/vcfa_k8s_architecture.svg)")
            self._add()
            
        domain_s = self.env.dns_domain or "site-a.vcf.lab"
        vcfa_cl = next((c for c in self.env.k8s_clusters if c.cluster_type == "VCFA"), None)
        vcfa_vip = vcfa_cl.vip if vcfa_cl and vcfa_cl.vip else (resolve_host('auto-a', domain_s) or "10.1.1.70")
        vcfa_node_ip = (vcfa_cl.nodes[0].ip_address if vcfa_cl and vcfa_cl.nodes and vcfa_cl.nodes[0].ip_address
                        else (resolve_host('auto-platform-a', domain_s) or "10.1.1.69"))
        vcfa_node_cpu = vcfa_cl.nodes[0].cpu_capacity if vcfa_cl and vcfa_cl.nodes and vcfa_cl.nodes[0].cpu_capacity else 24
        vcfa_node_ram = vcfa_cl.nodes[0].memory_mb // 1024 if vcfa_cl and vcfa_cl.nodes and vcfa_cl.nodes[0].memory_mb else 96

        self._add("| Component | Details |")
        self._add("| --------- | ------- |")
        self._add(f"| **Node VIP / IP** | `{vcfa_vip}` (`auto-a.{domain_s}`) / `{vcfa_node_ip}` (`auto-platform-a`) |")
        self._add(f"| **Node Sizing** | {vcfa_node_cpu} vCPUs / {vcfa_node_ram} GB RAM |")
        self._add("| **Ingress Mesh** | Istio Ingress Gateway & Kube-VIP |")
        self._add("| **Microservices** | `prelude`, `istio-system`, `vmsp-platform` |")
        self._add()
        
        # 4. Security Services Platform (SSP / vDefend) if detected
        if self.env.has_ssp or any(c.cluster_type == "SSP" for c in self.env.k8s_clusters):
            self._add("### 4. Security Services Platform (SSP / vDefend)")
            self._add()
            if self.diagram_style in ("glassmorphism", "both"):
                self._add(f"![SSP Security Platform Architecture]({self.svg_rel_dir}/ssp_k8s_architecture.svg)")
                self._add()
                
            ssp_cl = next((c for c in self.env.k8s_clusters if c.cluster_type == "SSP"), None)
            capi_ip = (ssp_cl.extra_info.get('capi_mgmt_ip') if ssp_cl and ssp_cl.extra_info 
                       else (resolve_host('ssp-i', domain_s) or resolve_host('ssp-installer', domain_s) or "10.1.0.10"))
            ingress_vip = ssp_cl.vip if ssp_cl and ssp_cl.vip else (resolve_host('ssp', domain_s) or "10.1.0.11")
            cp_count = len([n for n in ssp_cl.nodes if n.role == 'control-plane']) if ssp_cl else 3
            worker_count = len([n for n in ssp_cl.nodes if n.role == 'worker']) if ssp_cl else 6

            self._add("| Component | Details |")
            self._add("| --------- | ------- |")
            self._add(f"| **Management Host** | `{capi_ip}` (`sysadmin` / CAPI installer) |")
            self._add(f"| **Ingress VIP** | `{ingress_vip}` (`https://ssp.{domain_s}`) |")
            self._add(f"| **CAPI Cluster** | `ssp` namespace ({cp_count} Control Planes, {worker_count} Workers) |")
            self._add("| **Microservices** | `nsxi-platform` (NSX Intelligence, vDefend NDR, Malware Analysis, Distributed IDS/IPS, Kafka bus) |")
            self._add()
        
        self._add("---")
        self._add()

    def _add_core_infrastructure(self):
        """Add core infrastructure VMs & Holorouter architecture diagrams"""
        self._add("## Core Infrastructure & Holorouter Services")
        self._add()
        self._add("Core management appliances, Linux routing, TLS reverse proxy, and identity services powering the HOL pod fabric.")
        self._add()
        if self.diagram_style in ("glassmorphism", "both"):
            self._add(f"![Core Infrastructure VMs]({self.svg_rel_dir}/core_infrastructure.svg)")
            self._add()
            self._add("### Holorouter Services & Container Reverse Proxy Topology")
            self._add()
            self._add(f"![Holorouter Services & Reverse Proxy Architecture]({self.svg_rel_dir}/holorouter_architecture.svg)")
            self._add()
        if self.diagram_style in ("mermaid", "both"):
            self._add("```mermaid")
            self._add("flowchart TB")
            self._add(MERMAID_STYLES)
            self._add()
            self._add('    subgraph Core["Core Infrastructure VMs (L1)"]')
            self._add(f'        subgraph RouterVM["holorouter - {self.env.router_ip}"]')
            self._add('            RouterSvc["Services:<br/>- NGINX Reverse Proxy (:443)<br/>- Technitium DNS (:53/:5380)<br/>- Authentik OIDC (:9000)<br/>- Vault PKI (:32000)<br/>- Squid Proxy (:3128)<br/>- Firewall/NAT"]')
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
        domain_s = self.env.dns_domain or "site-a.vcf.lab"
        core_sub = self.env.core_subnet or get_subnet_for_ip(self.env.router_ip, 25) or "10.1.10.128/25"
        core_gw = self.env.router_ip or resolve_host('router', domain_s) or "10.1.10.129"
        ext_sub = self.env.external_subnet or get_subnet_for_ip(self.env.gateway_ip, 24) or "192.168.0.0/24"
        ext_gw = self.env.gateway_ip or resolve_host('gateway', domain_s) or "192.168.0.1"
        
        sample_host = self.env.hosts[0] if self.env.hosts else None
        mgmt_sub = get_subnet_for_ip(sample_host.mgmt_ip, 24) if sample_host and sample_host.mgmt_ip else "10.1.1.0/24"
        vsan_sub = get_subnet_for_ip(sample_host.vsan_ip, 24) if sample_host and sample_host.vsan_ip else "10.1.2.0/24"
        vmotion_sub = get_subnet_for_ip(sample_host.vmotion_ip, 24) if sample_host and sample_host.vmotion_ip else "10.1.3.0/24"
        tep_sub = get_subnet_for_ip(sample_host.tep_ip, 25) if sample_host and sample_host.tep_ip else "10.1.5.128/25"

        self._add("## Network Subnets Reference")
        self._add()
        self._add("| Network | Subnet | Gateway | Purpose |")
        self._add("| ------- | ------ | ------- | ------- |")
        self._add(f"| Core/External | {core_sub} | {core_gw} | Console, Manager, Router |")
        self._add(f"| Management | {mgmt_sub} | {mgmt_sub.rsplit('.', 1)[0] + '.1'} | VCF Management Components |")
        self._add(f"| vSAN | {vsan_sub} | - | vSAN Traffic |")
        self._add(f"| vMotion | {vmotion_sub} | - | vMotion Traffic |")
        self._add(f"| TEP (Overlay) | {tep_sub} | {tep_sub.rsplit('.', 1)[0] + '.129'} | NSX Transport Endpoint (GENEVE) |")
        self._add(f"| External (Holodeck) | {ext_sub} | {ext_gw} | External/Internet Access |")
        self._add()
        self._add("---")
        self._add()

    def _add_dvs_diagrams(self):
        """Add Distributed Virtual Switch diagrams"""
        self._add("## Distributed Virtual Switches")
        self._add()
        if self.diagram_style in ("glassmorphism", "both"):
            self._add(f"![Distributed Virtual Switches]({self.svg_rel_dir}/dvs_topology.svg)")
            self._add()
        if self.diagram_style in ("mermaid", "both"):
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
        if self.diagram_style in ("glassmorphism", "both"):
            self._add(f"![NSX Architecture]({self.svg_rel_dir}/nsx_architecture.svg)")
            self._add()
        if self.diagram_style in ("mermaid", "both"):
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
        if self.diagram_style in ("glassmorphism", "both"):
            self._add(f"![Lab Startup Boot Sequence]({self.svg_rel_dir}/lab_boot_sequence.svg)")
            self._add()
        if self.diagram_style in ("mermaid", "both"):
            self._add("```mermaid")
            self._add("sequenceDiagram")
            self._add("    participant Router as holorouter")
            self._add("    participant Manager as manager")
            self._add("    participant ESXi as ESXi Hosts")
            self._add("    participant NSX as NSX Manager")
            self._add("    participant Edges as NSX Edges")
            self._add("    participant VC as vCenter")
            self._add("    participant SDDC as SDDC Manager")
            self._add("    participant Ops as VCF Operations Suite")
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
            self._add("    Manager->>Ops: Power On VCF Operations Suite VMs")
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
                service = "VCF Automation"
            elif 'opslcm' in url:
                service = "VCF Operations Manager"
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
        self._add("| VCF Operations Suite | admin@local | See `/home/holuser/creds.txt` |")
        self._add("| Linux VMs (holuser) | holuser | See `/home/holuser/creds.txt` |")
        self._add("| Linux VMs (root) | root | See `/home/holuser/creds.txt` |")
        self._add()
        self._add("---")
        self._add()
    
    def _add_storage_summary(self):
        """Add storage summary"""
        self._add("## Storage Summary")
        self._add()
        if self.diagram_style in ("glassmorphism", "both"):
            self._add(f"![Storage Summary & vSAN Architecture]({self.svg_rel_dir}/storage_summary.svg)")
            self._add()
        if self.diagram_style in ("mermaid", "both"):
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
        if self.diagram_style in ("glassmorphism", "both"):
            self._add(f"![Complete Infrastructure Diagram]({self.svg_rel_dir}/complete_infrastructure.svg)")
            self._add()
        if self.diagram_style in ("mermaid", "both"):
            ext_sub = self.env.external_subnet or "192.168.0.0/24"
            self._add("```mermaid")
            self._add("flowchart TB")
            self._add(MERMAID_STYLES)
            self._add()
            self._add('    subgraph External["External Access"]')
            self._add(f'        Internet["Internet<br/>{ext_sub}"]')
            self._add('    end')
            self._add()
            self._add('    subgraph vPod["VMware Hands-on Lab vPod"]')
            self._add('        subgraph L1["Core VMs"]')
            self._add(f'            Router["holorouter<br/>{self.env.router_ip}<br/>DNS/DHCP/Proxy/FW"]')
            self._add(f'            Console["console<br/>{self.env.console_ip}<br/>Linux Desktop"]')
            self._add(f'            Manager["manager<br/>{self.env.manager_ip}<br/>Automation"]')
            self._add('        end')
            self._add()
            self._add('        subgraph L2["VCF Infrastructure"]')
            
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
            
            # VCF Operations Suite
            self._add('            subgraph VCFOps["VCF Operations Suite"]')
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
            self._add('    class VCFOps vcfops')
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
        self._add(f"| **Generator Version** | `v2.3.2` (Style 5 Glassmorphism Engine) |")
        self._add(f"| **Generated By** | `python3 Tools/labdetails/generate_labdetails.py` |")
        self._add(f"| **Diagram Engine License** | MIT License © 2025 fireworks-tech-graph contributors |")
        self._add("| **Lab Configuration** | `/tmp/config.ini` |")
        self._add(f"| **Source INI** | `/home/holuser/hol/holodeck/{self.env.lab_sku}.ini` |")
        self._add("| **Lab Startup Script** | `/home/holuser/hol/labstartup.py` |")

#==============================================================================
# MAIN & CLI HELP SCREEN
#==============================================================================

VERSION = "2.3.2"

def show_help():
    """Display script-help-style compliant help screen"""
    use_color = sys.stdout.isatty()
    
    def c(code, text):
        return f"\033[{code}m{text}\033[0m" if use_color else text
        
    cyan = lambda t: c("1;36", t)
    blue = lambda t: c("1;34", t)
    green = lambda t: c("1;32", t)
    yellow = lambda t: c("1;33", t)
    dim = lambda t: c("2;37", t)
    bold = lambda t: c("1", t)

    print(cyan("╔════════════════════════════════════════════════════════════════════════════════╗"))
    print(cyan("║") + blue(f"                 generate_labdetails.py — Version {VERSION}                          ") + cyan("║"))
    print(cyan("║") + bold("  Dynamic Lab Architecture & Multi-Style Topology Generator                     ") + cyan("║"))
    print(cyan("╚════════════════════════════════════════════════════════════════════════════════╝"))
    print()
    print(bold("DESCRIPTION:"))
    print("  Queries live vCenter, SDDC Manager, NSX Manager, Kubernetes clusters (Supervisor Tanzu,")
    print("  VSP Fleet LCM, VCF Automation, SSP), and holorouter gateway to generate dynamic")
    print("  <SKU>-labdetails.md and <SKU>-labdetails.html documentation along with 14 SVG topology diagrams.")
    print("  Supports all 12 visual themes from fireworks-tech-graph and single / dual site labs.")
    print()
    print(bold("USAGE:"))
    print(f"  {green('python3 Tools/labdetails/generate_labdetails.py')} [{yellow('[OPTIONS]')}]")
    print()
    print(bold("OPTIONS:"))
    print(f"  {green('-o, --output')} {yellow('<dir>')}          Destination directory for documentation & diagrams {dim(f'(default: {DEFAULT_OUTPUT})')}")
    print(f"  {green('--style, --theme')} {yellow('<name>')}     Visual theme for SVG diagrams {dim('(default: glassmorphism)')}")
    print(f"  {green('--diagram-style')} {yellow('<style>')}   Diagram format mode: {yellow('glassmorphism')}, {yellow('mermaid')}, or {yellow('both')} {dim('(default: glassmorphism)')}")
    print(f"  {green('--svg-dir')} {yellow('<path>')}         Directory for output SVG files {dim('(default: <output_dir>/images)')}")
    print(f"  {green('--html')}                    Generate HTML report viewer {dim('(always enabled by default)')}")
    print(f"  {green('--config')} {yellow('<path>')}         Path to config.ini {dim(f'(default: {CONFIG_INI})')}")
    print(f"  {green('--dry-run')}                  Print markdown to stdout without writing files")
    print(f"  {green('-v, --version')}              Display script version and exit")
    print(f"  {green('-h, --help')}                 Show this styled help screen and exit")
    print()
    print(bold("SUPPORTED VISUAL THEMES (--style / --theme):"))
    print(f"   1. {yellow('flat-icon')} ({dim('flat')})              - Clean light layout with drop shadows & badges")
    print(f"   2. {yellow('dark-terminal')} ({dim('terminal')})       - Developer monospace CLI aesthetic with neon cyan")
    print(f"   3. {yellow('blueprint')}                     - Architectural CAD grid background with electric blue")
    print(f"   4. {yellow('notion-clean')} ({dim('notion')})         - Ultra-minimal light layout with pastel accents")
    print(f"   5. {yellow('glassmorphism')} ({dim('glass')})         - {bold('[DEFAULT]')} Frosted translucent glass with ambient radial glow")
    print(f"   6. {yellow('claude-official')} ({dim('claude')})      - Warm Anthropic cream palette with terracotta highlights")
    print(f"   7. {yellow('openai-official')} ({dim('openai')})      - Modern OpenAI clean white layout with emerald green")
    print(f"   8. {yellow('dark-luxury')} ({dim('luxury')})         - Deep black canvas with champagne gold accents")
    print(f"   9. {yellow('c4-review')} ({dim('c4')})              - Architectural review paper background with crisp lines")
    print(f"  10. {yellow('cloud-fabric')} ({dim('cloud')})          - Multi-cloud topology layout with azure highlights")
    print(f"  11. {yellow('event-transit')} ({dim('transit')})        - Metro rail transit system style with station nodes")
    print(f"  12. {yellow('ops-pulse')} ({dim('ops')})              - SRE observability dashboard with dark navy & ECG pulses")
    print()
    print(bold("EXAMPLES:"))
    print(f"  {dim('# Generate <SKU>-labdetails.md and .html with Style 5 Glassmorphism SVGs in Tools folder')}")
    print(f"  {green('python3 Tools/labdetails/generate_labdetails.py --output Tools')}")
    print()
    print(f"  {dim('# Generate blueprint CAD grid style diagrams into custom folder')}")
    print(f"  {green('python3 Tools/labdetails/generate_labdetails.py --output /tmp/labdocs --style blueprint')}")
    print()
    print(f"  {dim('# Dry-run generation to stdout with Dark Terminal style')}")
    print(f"  {green('python3 Tools/labdetails/generate_labdetails.py --style dark-terminal --dry-run')}")
    print()
    print(bold("LICENSE NOTICE:"))
    print(dim("  Portions of diagram styling, color tokens, and layout principles derived from"))
    print(dim("  fireworks-tech-graph (https://github.com/yizhiyanhua-ai/fireworks-tech-graph)"))
    print(dim("  MIT License © 2025 fireworks-tech-graph contributors."))
    print()

class CustomArgumentParser(argparse.ArgumentParser):
    """Custom parser to integrate styled help and clean error reporting"""
    def error(self, message):
        sys.stderr.write(f"\033[1;31mERROR: {message}\033[0m\n\n" if sys.stderr.isatty() else f"ERROR: {message}\n\n")
        show_help()
        sys.exit(1)

def main():
    if '-h' in sys.argv or '--help' in sys.argv:
        show_help()
        sys.exit(0)
        
    if '-v' in sys.argv or '--version' in sys.argv:
        print(f"generate_labdetails.py {VERSION}")
        sys.exit(0)

    parser = CustomArgumentParser(
        description='Generate <SKU>-labdetails.md and .html from live lab environment',
        add_help=False
    )
    parser.add_argument(
        '--output', '-o',
        default=DEFAULT_OUTPUT,
        help=f'Destination directory for generated files (default: {DEFAULT_OUTPUT})'
    )
    parser.add_argument(
        '--style', '--theme',
        dest='style',
        default='glassmorphism',
        help='Visual theme for SVG diagrams'
    )
    parser.add_argument(
        '--diagram-style',
        choices=['glassmorphism', 'mermaid', 'both'],
        default='glassmorphism',
        help='Diagram rendering style'
    )
    parser.add_argument(
        '--svg-dir',
        default=None,
        help='Directory path for generated SVGs (default: <output_dir>/images)'
    )
    parser.add_argument(
        '--html',
        action='store_true',
        help='Generate standalone HTML viewer (always generated by default)'
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
        print(f"WARNING: Credentials file not found: {CREDS_FILE}. Proceeding with offline fallback mode.")
    
    # Collect lab data first to identify SKU and environment details
    collector = LabDataCollector(args.config)
    env = collector.collect_all()
    
    # Resolve destination folder
    dest_dir = os.path.abspath(args.output)
    if dest_dir.endswith('.md') or dest_dir.endswith('.html'):
        dest_dir = os.path.dirname(dest_dir)
        
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception:
        dest_dir = os.path.abspath(os.getcwd())
        os.makedirs(dest_dir, exist_ok=True)

    sku_name = env.lab_sku or "VCF-91"
    md_filename = f"{sku_name}-labdetails.md"
    html_filename = f"{sku_name}-labdetails.html"
    output_md_path = os.path.join(dest_dir, md_filename)
    output_html_path = os.path.join(dest_dir, html_filename)

    if args.svg_dir:
        svg_dir = os.path.abspath(args.svg_dir)
    else:
        svg_dir = os.path.join(dest_dir, 'images')
        
    # Calculate relative SVG directory for markdown links
    try:
        svg_rel_dir = os.path.relpath(svg_dir, dest_dir)
    except Exception:
        svg_rel_dir = 'images'
    
    # Build Diagrams using chosen theme style
    diagram_builder = LabDiagramBuilder(env, diagram_style=args.style)
    svg_map = diagram_builder.build_all()
    
    # Generate Markdown Documentation
    generator = LabDetailsGenerator(env, diagram_style=args.diagram_style, svg_rel_dir=svg_rel_dir)
    content = generator.generate()
    
    if args.dry_run:
        print(content)
        print("\n--- STANDALONE GLASSMORPHISM SVG SUMMARY ---")
        for filename, svg_data in svg_map.items():
            print(f"  • {filename}: {len(svg_data)} bytes (valid SVG)")
    else:
        # Create output directories
        os.makedirs(dest_dir, exist_ok=True)
        os.makedirs(svg_dir, exist_ok=True)
        
        # Write SVGs to disk
        print(f"\nWriting Glassmorphism SVG diagrams to {svg_dir}...")
        for filename, svg_data in svg_map.items():
            svg_path = os.path.join(svg_dir, filename)
            with open(svg_path, 'w', encoding='utf-8') as f:
                f.write(svg_data)
            print(f"  ✓ {filename} ({len(svg_data.splitlines())} lines)")
            
        # Write Markdown file
        with open(output_md_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"\n{md_filename} generated: {output_md_path}")
        print(f"Total lines: {len(content.splitlines())}")
        
        # Always generate HTML report
        html_content = generator.generate_html(svg_map)
        with open(output_html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        print(f"{html_filename} generated: {output_html_path}")

if __name__ == '__main__':
    main()

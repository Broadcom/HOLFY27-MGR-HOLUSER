#!/usr/bin/env python3
"""
generate_labdetails.py - Automatic Lab Documentation & Glassmorphism Topology Generator
Version 2.1 - August 2026
Author - HOL Core Team

License:
  Portions of diagram styling, color tokens, and layout principles derived from
  fireworks-tech-graph (https://github.com/yizhiyanhua-ai/fireworks-tech-graph)
  MIT License © 2025 fireworks-tech-graph contributors.

Generates a comprehensive LABDETAILS.md file with standalone Style 5 Glassmorphism SVG diagrams
and Mermaid diagrams by querying live vCenter, NSX, and SDDC Manager environments.

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
from xml.sax.saxutils import escape
from configparser import ConfigParser
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Tuple

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

def xml_escape(val: Any) -> str:
    """Safely escape text content for XML/SVG rendering"""
    if val is None:
        return ""
    return escape(str(val))

#==============================================================================
# STYLE 5 GLASSMORPHISM SVG ENGINE
# Portions derived from fireworks-tech-graph (MIT License © 2025)
#==============================================================================

@dataclass
class GlassCard:
    """Represents a Glassmorphic node card"""
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
    Pure-Python Standalone Style 5 Glassmorphism SVG Builder Engine.
    Encodes frosted glass cards, ambient radial glows, translucent containers,
    and glowing semantic data flow paths.
    """
    COLOR_BLUE = "#58a6ff"
    COLOR_PURPLE = "#bc8cff"
    COLOR_GREEN = "#3fb950"
    COLOR_ORANGE = "#f78166"
    COLOR_AMBER = "#d29922"
    COLOR_CYAN = "#38bdf8"
    COLOR_MUTED = "#8b949e"
    
    def __init__(self, width: int = 1000, height: int = 700, title: str = "", subtitle: str = ""):
        self.width = width
        self.height = height
        self.title = title
        self.subtitle = subtitle
        self.lines: List[str] = []
        self.containers: List[Dict[str, Any]] = []
        self.cards: List[GlassCard] = []
        self.edges: List[FlowEdge] = []
        self.legends: List[Tuple[str, str]] = []
        
    def _render_defs(self):
        """Render SVG defs, styles, gradients, filters, and markers"""
        self.lines.append('  <defs>')
        self.lines.append('    <style>')
        self.lines.append('      @import url("https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&amp;display=swap");')
        self.lines.append('      text { font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }')
        self.lines.append('      .hero-title { font-size: 20px; font-weight: 700; fill: url(#title-grad); }')
        self.lines.append('      .hero-subtitle { font-size: 12px; fill: #8b949e; }')
        self.lines.append('      .card-title { font-size: 13px; font-weight: 600; fill: #f0f6fc; }')
        self.lines.append('      .card-subtitle { font-size: 11px; fill: #8b949e; }')
        self.lines.append('      .card-detail { font-size: 10.5px; fill: #c9d1d9; }')
        self.lines.append('      .container-title { font-size: 12px; font-weight: 600; fill: #e6edf3; }')
        self.lines.append('      .edge-label { font-size: 10px; font-weight: 600; fill: #f0f6fc; }')
        self.lines.append('      .badge-text { font-size: 9.5px; font-weight: 600; fill: #0d1117; }')
        self.lines.append('    </style>')
        
        # Background gradient
        self.lines.append('    <linearGradient id="bg-grad" x1="0%" y1="0%" x2="100%" y2="100%">')
        self.lines.append('      <stop offset="0%" stop-color="#0d1117"/>')
        self.lines.append('      <stop offset="50%" stop-color="#161b22"/>')
        self.lines.append('      <stop offset="100%" stop-color="#0d1117"/>')
        self.lines.append('    </linearGradient>')
        
        # Hero title text gradient
        self.lines.append('    <linearGradient id="title-grad" x1="0%" y1="0%" x2="100%" y2="0%">')
        self.lines.append('      <stop offset="0%" stop-color="#58a6ff"/>')
        self.lines.append('      <stop offset="100%" stop-color="#bc8cff"/>')
        self.lines.append('    </linearGradient>')
        
        # Radial ambient glows
        self.lines.append('    <radialGradient id="glow-blue" cx="30%" cy="30%" r="50%">')
        self.lines.append('      <stop offset="0%" stop-color="rgba(88,166,255,0.15)"/>')
        self.lines.append('      <stop offset="100%" stop-color="rgba(88,166,255,0)"/>')
        self.lines.append('    </radialGradient>')
        self.lines.append('    <radialGradient id="glow-purple" cx="75%" cy="65%" r="45%">')
        self.lines.append('      <stop offset="0%" stop-color="rgba(188,140,255,0.12)"/>')
        self.lines.append('      <stop offset="100%" stop-color="rgba(188,140,255,0)"/>')
        self.lines.append('    </radialGradient>')
        self.lines.append('    <radialGradient id="glow-green" cx="20%" cy="80%" r="40%">')
        self.lines.append('      <stop offset="0%" stop-color="rgba(63,185,80,0.10)"/>')
        self.lines.append('      <stop offset="100%" stop-color="rgba(63,185,80,0)"/>')
        self.lines.append('    </radialGradient>')
        self.lines.append('    <radialGradient id="glow-orange" cx="80%" cy="20%" r="40%">')
        self.lines.append('      <stop offset="0%" stop-color="rgba(247,129,102,0.10)"/>')
        self.lines.append('      <stop offset="100%" stop-color="rgba(247,129,102,0)"/>')
        self.lines.append('    </radialGradient>')
        
        # Filters
        self.lines.append('    <filter id="glass-shadow" x="-10%" y="-10%" width="120%" height="130%">')
        self.lines.append('      <feDropShadow dx="0" dy="6" stdDeviation="10" flood-color="#000000" flood-opacity="0.35"/>')
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
                      subtitle: str = "", icon: str = "📦", border_color: str = "rgba(255,255,255,0.12)", 
                      fill: str = "rgba(255,255,255,0.02)", dashed: bool = False, accent_color: str = None):
        """Add a translucent grouping container rectangle"""
        self.containers.append({
            "x": x, "y": y, "width": width, "height": height,
            "title": title, "subtitle": subtitle, "icon": icon,
            "border_color": border_color, "fill": fill, "dashed": dashed,
            "accent_color": accent_color or self.COLOR_BLUE
        })
        
    def add_card(self, card: GlassCard):
        """Add a Glass Card node"""
        self.cards.append(card)
        
    def add_edge(self, edge: FlowEdge):
        """Add a glowing flow edge"""
        self.edges.append(edge)
        
    def add_legend(self, items: List[Tuple[str, str]]):
        """Add legend items: list of (label, color_hex)"""
        self.legends = items

    def render(self) -> str:
        """Assemble and return complete valid SVG string"""
        self.lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.width} {self.height}" width="{self.width}" height="{self.height}">',
        ]
        
        self._render_defs()
        
        # Layer 1: Background Rect & Glows
        self.lines.append(f'  <rect width="{self.width}" height="{self.height}" fill="url(#bg-grad)"/>')
        self.lines.append(f'  <rect width="{self.width}" height="{self.height}" fill="url(#glow-blue)"/>')
        self.lines.append(f'  <rect width="{self.width}" height="{self.height}" fill="url(#glow-purple)"/>')
        self.lines.append(f'  <rect width="{self.width}" height="{self.height}" fill="url(#glow-green)"/>')
        self.lines.append(f'  <rect width="{self.width}" height="{self.height}" fill="url(#glow-orange)"/>')
        
        # Layer 2: Title Block
        if self.title:
            self.lines.append('  <g transform="translate(40, 36)">')
            self.lines.append(f'    <text class="hero-title" x="0" y="0">{xml_escape(self.title)}</text>')
            if self.subtitle:
                self.lines.append(f'    <text class="hero-subtitle" x="0" y="18">{xml_escape(self.subtitle)}</text>')
            self.lines.append('  </g>')
            
        # Layer 3: Containers
        for c in self.containers:
            dash_attr = ' stroke-dasharray="6,4"' if c["dashed"] else ''
            self.lines.append(f'  <g id="container-{xml_escape(c["title"]).replace(" ", "_")}">')
            self.lines.append(f'    <rect x="{c["x"]}" y="{c["y"]}" width="{c["width"]}" height="{c["height"]}" rx="14" ry="14" fill="{c["fill"]}" stroke="{c["border_color"]}" stroke-width="1.2"{dash_attr}/>')
            
            # Header pill badge
            pill_w = max(120, len(c["title"]) * 7.5 + 40)
            self.lines.append(f'    <rect x="{c["x"] + 12}" y="{c["y"] - 12}" width="{pill_w}" height="24" rx="12" fill="#161b22" stroke="{c["border_color"]}" stroke-width="1"/>')
            icon_str = f'{xml_escape(c["icon"])} ' if c["icon"] else ''
            self.lines.append(f'    <text class="container-title" x="{c["x"] + 24}" y="{c["y"] + 4}">{icon_str}{xml_escape(c["title"])}</text>')
            if c["subtitle"]:
                self.lines.append(f'    <text class="card-subtitle" x="{c["x"] + c["width"] - 16}" y="{c["y"] + 16}" text-anchor="end">{xml_escape(c["subtitle"])}</text>')
            self.lines.append('  </g>')

        # Layer 4: Edges & Glowing Lines (drawn before cards so labels/card bodies cleanly overlay)
        for e in self.edges:
            dash = ' stroke-dasharray="6,3"' if e.dashed else ''
            
            # Select marker
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
            
            # Path data building
            pts = [e.start] + e.waypoints + [e.end]
            path_d = f"M {pts[0][0]:.1f},{pts[0][1]:.1f}"
            for p in pts[1:]:
                path_d += f" L {p[0]:.1f},{p[1]:.1f}"
                
            self.lines.append('  <g>')
            # Outer glow casing
            self.lines.append(f'    <path d="{path_d}" fill="none" stroke="{e.color}" stroke-width="{e.stroke_width + 2}" stroke-opacity="0.25"/>')
            # Main path
            self.lines.append(f'    <path d="{path_d}" fill="none" stroke="{e.color}" stroke-width="{e.stroke_width}" stroke-opacity="0.9"{dash}{marker_str}/>')
            
            # Label badge mid-path
            if e.label:
                # Find midpoint
                if len(pts) == 2:
                    mx = (pts[0][0] + pts[1][0]) / 2.0
                    my = (pts[0][1] + pts[1][1]) / 2.0
                else:
                    mid_idx = len(pts) // 2
                    mx, my = pts[mid_idx]
                
                lbl_text = xml_escape(e.label)
                lbl_w = max(60.0, len(lbl_text) * 6.5 + 16.0)
                self.lines.append(f'    <rect x="{mx - lbl_w/2:.1f}" y="{my - 10:.1f}" width="{lbl_w:.1f}" height="20" rx="6" fill="#0d1117" fill-opacity="0.92" stroke="{e.color}" stroke-width="0.8"/>')
                self.lines.append(f'    <text class="edge-label" x="{mx:.1f}" y="{my + 4:.1f}" text-anchor="middle">{lbl_text}</text>')
            self.lines.append('  </g>')

        # Layer 5: Glass Cards
        for card in self.cards:
            self.lines.append(f'  <g id="card-{xml_escape(card.id)}" filter="url(#glass-shadow)">')
            # Outer subtle glow border
            self.lines.append(f'    <rect x="{card.x}" y="{card.y}" width="{card.width}" height="{card.height}" rx="12" ry="12" fill="rgba(255,255,255,0.04)" stroke="rgba(255,255,255,0.15)" stroke-width="1.2"/>')
            # Top highlight line
            self.lines.append(f'    <line x1="{card.x + 12}" y1="{card.y + 1}" x2="{card.x + card.width - 12}" y2="{card.y + 1}" stroke="rgba(255,255,255,0.30)" stroke-width="1"/>')
            
            # Left accent pill/bar
            if card.accent_color:
                self.lines.append(f'    <rect x="{card.x + 1}" y="{card.y + 12}" width="3.5" height="{max(12.0, card.height - 24)}" rx="1.7" fill="{card.accent_color}"/>')
            
            # Icon & Title
            curr_y = card.y + 22
            icon_prefix = f'{xml_escape(card.icon)} ' if card.icon else ''
            self.lines.append(f'    <text class="card-title" x="{card.x + 14}" y="{curr_y}">{icon_prefix}{xml_escape(card.title)}</text>')
            
            # Status badge (top-right of card)
            if card.status_badge:
                badge_w = max(40.0, len(card.status_badge) * 6.0 + 12.0)
                bx = card.x + card.width - badge_w - 10
                by = card.y + 10
                self.lines.append(f'    <rect x="{bx}" y="{by}" width="{badge_w}" height="18" rx="9" fill="{card.badge_color}"/>')
                self.lines.append(f'    <text class="badge-text" x="{bx + badge_w/2}" y="{by + 12.5}" text-anchor="middle">{xml_escape(card.status_badge)}</text>')

            if card.subtitle:
                curr_y += 16
                self.lines.append(f'    <text class="card-subtitle" x="{card.x + 14}" y="{curr_y}">{xml_escape(card.subtitle)}</text>')
                
            # Details lines
            if card.details:
                curr_y += 14
                for d in card.details:
                    curr_y += 14
                    if curr_y < card.y + card.height - 6:
                        self.lines.append(f'    <text class="card-detail" x="{card.x + 14}" y="{curr_y}">• {xml_escape(d)}</text>')
            self.lines.append('  </g>')

        # Layer 6: Legend (if defined)
        if self.legends:
            leg_x = self.width - 200
            leg_y = 24
            leg_w = 170
            leg_h = len(self.legends) * 20 + 20
            self.lines.append('  <g id="legend">')
            self.lines.append(f'    <rect x="{leg_x}" y="{leg_y}" width="{leg_h if leg_w < 170 else leg_w}" height="{leg_h}" rx="8" fill="#161b22" stroke="rgba(255,255,255,0.15)" stroke-width="1"/>')
            self.lines.append(f'    <text class="container-title" x="{leg_x + 12}" y="{leg_y + 16}">Legend / Planes</text>')
            iy = leg_y + 34
            for label, col in self.legends:
                self.lines.append(f'    <circle cx="{leg_x + 18}" cy="{iy - 4}" r="4.5" fill="{col}"/>')
                self.lines.append(f'    <text class="card-detail" x="{leg_x + 30}" y="{iy}">{xml_escape(label)}</text>')
                iy += 18
            self.lines.append('  </g>')

        self.lines.append('</svg>')
        return '\n'.join(self.lines)

class LabDiagramBuilder:
    """
    Constructs 6 specialized Style 5 Glassmorphism SVG diagrams
    illustrating connectivity and data flow throughout the VCF lab environment.
    """
    def __init__(self, env: LabEnvironment):
        self.env = env

    def build_high_level_architecture(self) -> GlassmorphismCanvas:
        """1. High-Level Lab Architecture & Ingress/Egress Connectivity"""
        c = GlassmorphismCanvas(
            width=1050, height=700,
            title="High-Level Lab Architecture & Connectivity",
            subtitle=f"SKU: {self.env.lab_sku or 'VCF-91'} | Type: {self.env.lab_type or 'DISCOVERY'} | Domain: {self.env.dns_domain or 'site-a.vcf.lab'}"
        )
        c.add_legend([
            ("Core / Ingress", GlassmorphismCanvas.COLOR_BLUE),
            ("Control Plane", GlassmorphismCanvas.COLOR_PURPLE),
            ("Workload Plane", GlassmorphismCanvas.COLOR_AMBER),
            ("Gateway / External", GlassmorphismCanvas.COLOR_MUTED),
        ])
        
        # Containers
        c.add_container(40, 80, 230, 580, "External Network", subtitle="192.168.0.0/24", icon="🌐")
        c.add_container(300, 80, 230, 580, "Core Infrastructure", subtitle="10.1.10.128/25", icon="🛠️", accent_color=GlassmorphismCanvas.COLOR_BLUE)
        c.add_container(560, 80, 450, 580, "VMware Cloud Foundation", subtitle="SDDC & Workload Fabric", icon="☁️", accent_color=GlassmorphismCanvas.COLOR_PURPLE)
        
        c.add_container(580, 115, 410, 260, "Management Domain: mgmt-a", subtitle="10.1.1.0/24", icon="🏛️", border_color="rgba(188,140,255,0.25)")
        c.add_container(580, 390, 410, 250, "Workload Domain: wld01-a", subtitle="10.1.1.0/24", icon="⚡", border_color="rgba(210,153,34,0.25)")
        
        # Nodes - External
        c.add_card(GlassCard("ext-gateway", 65, 130, 180, 100, "External Gateway", "192.168.0.1", "🌐", "UP", GlassmorphismCanvas.COLOR_GREEN, ["Internet Access", "Proxy Uplink"], GlassmorphismCanvas.COLOR_MUTED))
        c.add_card(GlassCard("ext-dns", 65, 270, 180, 90, "External DNS", "10.1.10.129", "🔍", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Technitium DNS", "Upstream Resolver"], GlassmorphismCanvas.COLOR_MUTED))
        
        # Nodes - Core
        r_ip = self.env.router_ip or "10.1.10.129"
        con_ip = self.env.console_ip or "10.1.10.130"
        mgr_ip = self.env.manager_ip or "10.1.10.131"
        c.add_card(GlassCard("holorouter", 325, 130, 180, 115, "holorouter", r_ip, "🛡️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["DNS / DHCP / NTP", "Squid Proxy (:3128)", "NAT & Firewall"], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("console", 325, 275, 180, 100, "console", con_ip, "🖥️", "READY", GlassmorphismCanvas.COLOR_GREEN, ["Ubuntu Desktop", "Firefox Browser", "SSH / VNC Client"], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("manager", 325, 405, 180, 100, "manager", mgr_ip, "🚀", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Lab Startup Engine", "Python Automation", "NFS /tmp Export"], GlassmorphismCanvas.COLOR_BLUE))
        
        # Nodes - VCF Mgmt
        c.add_card(GlassCard("sddc", 600, 155, 175, 95, "SDDC Manager", "sddcmanager-a", "🎛️", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, ["VCF Lifecycle API", "vsphere.local SSO"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("vc-mgmt", 795, 155, 175, 95, "vCenter Mgmt", "vc-mgmt-a", "🏢", "RUNNING", GlassmorphismCanvas.COLOR_GREEN, ["VAMI :5480", "cluster-mgmt-01a"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("nsx-mgmt", 600, 265, 175, 90, "NSX Manager", "nsx-mgmt-01a", "🔀", "READY", GlassmorphismCanvas.COLOR_GREEN, ["Network Virtualization", "Tier-0 / Tier-1"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("mgmt-hosts", 795, 265, 175, 90, "Mgmt ESXi Cluster", "4 Hosts (esx-01..04)", "🖥️", "4/4 UP", GlassmorphismCanvas.COLOR_GREEN, ["10.1.1.101 - 104", "vSAN Datastore"], GlassmorphismCanvas.COLOR_PURPLE))
        
        # Nodes - VCF Wld
        c.add_card(GlassCard("vc-wld", 600, 430, 175, 90, "vCenter Wld", "vc-wld01-a", "🏬", "RUNNING", GlassmorphismCanvas.COLOR_GREEN, ["wld.sso Domain", "cluster-wld01-01a"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("nsx-wld", 795, 430, 175, 90, "NSX Wld", "nsx-wld01-01a", "🔀", "READY", GlassmorphismCanvas.COLOR_GREEN, ["GENEVE Overlay", "Edge Clusters"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("wld-hosts", 600, 535, 370, 80, "Workload ESXi Cluster", "3 Hosts (esx-05a .. esx-07a)", "🖥️", "3/3 UP", GlassmorphismCanvas.COLOR_GREEN, ["10.1.1.105 - 107", "vSAN Capacity Fabric"], GlassmorphismCanvas.COLOR_AMBER))
        
        # Edges
        c.add_edge(FlowEdge((245, 180), (325, 180), "NAT / Proxy", GlassmorphismCanvas.COLOR_MUTED))
        c.add_edge(FlowEdge((415, 245), (415, 275), "Local LAN", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((415, 375), (415, 405), "Control", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((505, 455), (600, 200), "VCF APIs", GlassmorphismCanvas.COLOR_PURPLE, waypoints=[(540, 455), (540, 200)]))
        c.add_edge(FlowEdge((775, 200), (795, 200), "vSphere API", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((685, 250), (685, 265), "Management", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((685, 200), (600, 475), "Wld Provision", GlassmorphismCanvas.COLOR_AMBER, waypoints=[(530, 200), (530, 475)]))
        
        return c

    def build_network_dataflow(self) -> GlassmorphismCanvas:
        """2. Multi-Plane Network & Data Flow Topology"""
        c = GlassmorphismCanvas(
            width=1080, height=750,
            title="Multi-Plane Network & Data Flow Topology",
            subtitle="Isolation & Traffic Flow across 5 Physical/Virtual Planes"
        )
        c.add_legend([
            ("Plane 1: Core/Admin", GlassmorphismCanvas.COLOR_BLUE),
            ("Plane 2: Mgmt Control", GlassmorphismCanvas.COLOR_PURPLE),
            ("Plane 3: vSAN Fabric", GlassmorphismCanvas.COLOR_GREEN),
            ("Plane 4: vMotion Fabric", GlassmorphismCanvas.COLOR_CYAN),
            ("Plane 5: NSX GENEVE TEP", GlassmorphismCanvas.COLOR_ORANGE),
        ])
        
        # Containers for Planes
        c.add_container(40, 80, 990, 110, "Plane 1: Core & Services Subnet", subtitle="10.1.10.128/25", icon="⚡", border_color="rgba(88,166,255,0.3)")
        c.add_container(40, 215, 990, 115, "Plane 2: VCF Management Subnet", subtitle="10.1.1.0/24", icon="🏛️", border_color="rgba(188,140,255,0.3)")
        c.add_container(40, 350, 485, 115, "Plane 3: vSAN Storage Subnet", subtitle="10.1.2.0/24", icon="💾", border_color="rgba(63,185,80,0.3)")
        c.add_container(545, 350, 485, 115, "Plane 4: vMotion Live Migration", subtitle="10.1.3.0/24", icon="🔄", border_color="rgba(56,189,248,0.3)")
        c.add_container(40, 485, 990, 240, "Plane 5: NSX GENEVE Overlay TEP Subnet", subtitle="10.1.5.128/25", icon="🔀", border_color="rgba(247,129,102,0.3)")
        
        # Nodes Plane 1
        r_ip = self.env.router_ip or "10.1.10.129"
        m_ip = self.env.manager_ip or "10.1.10.131"
        c.add_card(GlassCard("p1-router", 70, 115, 200, 65, "holorouter", r_ip, "🛡️", "GW .129", GlassmorphismCanvas.COLOR_GREEN, ["DNS/DHCP Server"], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("p1-console", 430, 115, 200, 65, "console", "10.1.10.130", "🖥️", "IP .130", GlassmorphismCanvas.COLOR_GREEN, ["Management UI"], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("p1-manager", 790, 115, 200, 65, "manager", m_ip, "🚀", "IP .131", GlassmorphismCanvas.COLOR_GREEN, ["Automation Engine"], GlassmorphismCanvas.COLOR_BLUE))
        
        # Nodes Plane 2
        c.add_card(GlassCard("p2-sddc", 70, 250, 210, 65, "SDDC Manager", "10.1.1.x", "🎛️", "VIP .17", GlassmorphismCanvas.COLOR_GREEN, ["LCM Control"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("p2-vcmgmt", 310, 250, 210, 65, "vc-mgmt-a", "10.1.1.16", "🏢", "IP .16", GlassmorphismCanvas.COLOR_GREEN, ["vCenter Mgmt"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("p2-vcwld", 550, 250, 210, 65, "vc-wld01-a", "10.1.1.26", "🏬", "IP .26", GlassmorphismCanvas.COLOR_GREEN, ["vCenter Wld"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("p2-nsx", 790, 250, 200, 65, "NSX Managers", "10.1.1.x", "🔀", "VIP .11", GlassmorphismCanvas.COLOR_GREEN, ["Control Cluster"], GlassmorphismCanvas.COLOR_PURPLE))
        
        # Nodes Plane 3 & 4
        c.add_card(GlassCard("p3-vsan", 70, 385, 425, 65, "vSAN Cluster Storage Fabric", "10.1.2.101 - 10.1.2.107", "💾", "NVMe/SSD", GlassmorphismCanvas.COLOR_GREEN, ["Kernel vmk1 | Dedicated vSAN Network"], GlassmorphismCanvas.COLOR_GREEN))
        c.add_card(GlassCard("p4-vmotion", 575, 385, 425, 65, "vMotion Migration Fabric", "10.1.3.101 - 10.1.3.107", "🔄", "10 GbE", GlassmorphismCanvas.COLOR_GREEN, ["Kernel vmk2 | Live VM Storage/State"], GlassmorphismCanvas.COLOR_CYAN))
        
        # Nodes Plane 5
        c.add_card(GlassCard("p5-tn-mgmt", 70, 525, 425, 80, "Mgmt ESXi Transport Nodes", "10.1.5.131 - 10.1.5.134", "🖥️", "4 Nodes", GlassmorphismCanvas.COLOR_GREEN, ["Kernel vmk50 | GENEVE Tunnel Endpoints"], GlassmorphismCanvas.COLOR_ORANGE))
        c.add_card(GlassCard("p5-tn-wld", 575, 525, 425, 80, "Wld ESXi Transport Nodes", "10.1.5.135 - 10.1.5.137", "🖥️", "3 Nodes", GlassmorphismCanvas.COLOR_GREEN, ["Kernel vmk50 | GENEVE Tunnel Endpoints"], GlassmorphismCanvas.COLOR_ORANGE))
        c.add_card(GlassCard("p5-edges", 250, 630, 550, 75, "NSX Edge Node Cluster", "10.1.5.141 - 10.1.5.144 (TEP IPs)", "🛡️", "Active/Standby", GlassmorphismCanvas.COLOR_GREEN, ["Tier-0/Tier-1 Uplinks & BGP Routing"], GlassmorphismCanvas.COLOR_ORANGE))
        
        # Flow Edges connecting planes
        c.add_edge(FlowEdge((270, 147), (430, 147), "DHCP/DNS", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((630, 147), (790, 147), "SSH/API", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((175, 180), (175, 250), "Routing", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((280, 282), (310, 282), "SDDC API", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((520, 282), (550, 282), "Federation", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((760, 282), (790, 282), "Plugin", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((280, 450), (280, 525), "vSAN Sync", GlassmorphismCanvas.COLOR_GREEN))
        c.add_edge(FlowEdge((787, 450), (787, 525), "vMotion Sync", GlassmorphismCanvas.COLOR_CYAN))
        c.add_edge(FlowEdge((280, 605), (350, 630), "GENEVE Tunnel", GlassmorphismCanvas.COLOR_ORANGE, waypoints=[(280, 618), (350, 618)]))
        c.add_edge(FlowEdge((787, 605), (700, 630), "GENEVE Tunnel", GlassmorphismCanvas.COLOR_ORANGE, waypoints=[(787, 618), (700, 618)]))

        return c

    def build_vcf_domain_architecture(self) -> GlassmorphismCanvas:
        """3. VCF Domain Hierarchy & Control Plane Topology"""
        c = GlassmorphismCanvas(
            width=1080, height=760,
            title="VCF Domain Hierarchy & Control Plane Topology",
            subtitle="SDDC Manager Orchestration across Management & Workload Domains"
        )
        c.add_legend([
            ("Management Domain", GlassmorphismCanvas.COLOR_PURPLE),
            ("Workload Domain", GlassmorphismCanvas.COLOR_AMBER),
            ("SDDC Orchestrator", GlassmorphismCanvas.COLOR_BLUE),
        ])
        
        # SDDC Manager Orchestrator Card
        c.add_card(GlassCard("sddc-top", 430, 80, 220, 105, "SDDC Manager", "sddcmanager-a.site-a.vcf.lab", "🎛️", "VCF 9.1", GlassmorphismCanvas.COLOR_GREEN, ["SSO: vsphere.local", "Domain & Cluster LCM", "REST API Engine"], GlassmorphismCanvas.COLOR_BLUE))
        
        # Management Domain Container
        c.add_container(40, 215, 485, 515, "Management Domain: mgmt-a", subtitle="System Control Plane", icon="🏛️", border_color="rgba(188,140,255,0.3)")
        
        # Mgmt Domain Cards
        c.add_card(GlassCard("vc-mgmt-d", 65, 255, 435, 95, "vCenter Server (Management)", "vc-mgmt-a.site-a.vcf.lab", "🏢", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["SSO Domain: vsphere.local", "Datacenter: dc-a | Cluster: cluster-mgmt-01a"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("nsx-mgmt-d", 65, 365, 435, 95, "NSX Manager Cluster", "nsx-mgmt-01a.site-a.vcf.lab (VIP)", "🔀", "HA READY", GlassmorphismCanvas.COLOR_GREEN, ["Management Overlay & Firewall Policies", "Transport Nodes: esx-01a .. esx-04a"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("cl-mgmt-d", 65, 475, 435, 110, "Cluster: cluster-mgmt-01a", "4 ESXi Hosts (esx-01a to esx-04a)", "🖥️", "vSAN ON", GlassmorphismCanvas.COLOR_GREEN, ["CPU Cores: 128 Total | RAM: 512 GB Total", "Management VMs: SDDC, vCenter, NSX, VCF Ops"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("ds-mgmt-d", 65, 600, 435, 95, "vSAN Datastore: vsan-cluster-mgmt-01a", "Type: vSAN Flash | Capacity: ~12.0 TB", "💾", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, ["Resiliency: FTT=1 (RAID-1 Mirroring)", "Storage Policy: VCF Default Management"], GlassmorphismCanvas.COLOR_PURPLE))
        
        # Workload Domain Container
        c.add_container(555, 215, 485, 515, "Workload Domain: wld01-a", subtitle="Tenant Workload Fabric", icon="⚡", border_color="rgba(210,153,34,0.3)")
        
        # Wld Domain Cards
        c.add_card(GlassCard("vc-wld-d", 580, 255, 435, 95, "vCenter Server (Workload)", "vc-wld01-a.site-a.vcf.lab", "🏬", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["SSO Domain: wld.sso (Isolated SSO)", "Datacenter: dc-wld01 | Cluster: cluster-wld01-01a"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("nsx-wld-d", 580, 365, 435, 95, "NSX Manager Cluster (Workload)", "nsx-wld01-01a.site-a.vcf.lab", "🔀", "HA READY", GlassmorphismCanvas.COLOR_GREEN, ["Tenant Overlay & Micro-segmentation", "Transport Nodes: esx-05a .. esx-07a"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("cl-wld-d", 580, 475, 435, 110, "Cluster: cluster-wld01-01a", "3 ESXi Hosts (esx-05a to esx-07a)", "🖥️", "vSAN ON", GlassmorphismCanvas.COLOR_GREEN, ["CPU Cores: 96 Total | RAM: 384 GB Total", "Supervisor & Tanzu K8s Workload Pods"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("ds-wld-d", 580, 600, 435, 95, "vSAN Datastore: vsan-cluster-wld01-01a", "Type: vSAN Flash | Capacity: ~10.0 TB", "💾", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, ["Resiliency: FTT=1 (RAID-1 Mirroring)", "Storage Policy: VCF Workload Default"], GlassmorphismCanvas.COLOR_AMBER))

        # Flow Edges from SDDC Manager to Domains
        c.add_edge(FlowEdge((430, 130), (280, 255), "Mgmt Orchestration", GlassmorphismCanvas.COLOR_PURPLE, waypoints=[(280, 130)]))
        c.add_edge(FlowEdge((650, 130), (800, 255), "Wld Orchestration", GlassmorphismCanvas.COLOR_AMBER, waypoints=[(800, 130)]))
        c.add_edge(FlowEdge((280, 350), (280, 365), "Inventory Sync", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((280, 460), (280, 475), "Host Control", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((280, 585), (280, 600), "vSAN Claim", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((797, 350), (797, 365), "Inventory Sync", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((797, 460), (797, 475), "Host Control", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((797, 585), (797, 600), "vSAN Claim", GlassmorphismCanvas.COLOR_AMBER))

        return c

    def build_esxi_host_layout(self) -> GlassmorphismCanvas:
        """4. ESXi Physical Host & Interface Fabric"""
        c = GlassmorphismCanvas(
            width=1100, height=720,
            title="ESXi Physical Host & Interface Fabric",
            subtitle="7 ESXi Hosts across Management & Workload Clusters with Multi-NIC Interfaces"
        )
        c.add_legend([
            ("Management Host", GlassmorphismCanvas.COLOR_PURPLE),
            ("Workload Host", GlassmorphismCanvas.COLOR_AMBER),
        ])
        
        # Container Mgmt Cluster (Hosts 1 to 4)
        c.add_container(30, 80, 1040, 290, "Management Cluster: cluster-mgmt-01a", subtitle="4 ESXi Hosts (10.1.1.101 - 104)", icon="🖥️", border_color="rgba(188,140,255,0.3)")
        
        mgmt_hosts = [
            ("esx-01a", "10.1.1.101", "10.1.2.101", "10.1.3.101", "10.1.5.131"),
            ("esx-02a", "10.1.1.102", "10.1.2.102", "10.1.3.102", "10.1.5.132"),
            ("esx-03a", "10.1.1.103", "10.1.2.103", "10.1.3.103", "10.1.5.133"),
            ("esx-04a", "10.1.1.104", "10.1.2.104", "10.1.3.104", "10.1.5.134"),
        ]
        
        x_pos = 55
        for fqdn, m_ip, v_ip, vm_ip, t_ip in mgmt_hosts:
            c.add_card(GlassCard(
                f"card-{fqdn}", x_pos, 115, 235, 235, f"{fqdn}.site-a.vcf.lab", "ESXi 8.0 U3", "🖥️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN,
                [
                    "Specs: 32 Cores | 128 GB",
                    f"MGMT: {m_ip}",
                    f"vSAN: {v_ip}",
                    f"vMotion: {vm_ip}",
                    f"TEP: {t_ip}",
                    "DS: vsan-cluster-mgmt-01a"
                ],
                GlassmorphismCanvas.COLOR_PURPLE
            ))
            x_pos += 255

        # Container Wld Cluster (Hosts 5 to 7)
        c.add_container(30, 395, 1040, 290, "Workload Cluster: cluster-wld01-01a", subtitle="3 ESXi Hosts (10.1.1.105 - 107)", icon="⚡", border_color="rgba(210,153,34,0.3)")
        
        wld_hosts = [
            ("esx-05a", "10.1.1.105", "10.1.2.105", "10.1.3.105", "10.1.5.135"),
            ("esx-06a", "10.1.1.106", "10.1.2.106", "10.1.3.106", "10.1.5.136"),
            ("esx-07a", "10.1.1.107", "10.1.2.107", "10.1.3.107", "10.1.5.137"),
        ]
        
        x_pos = 55
        for fqdn, m_ip, v_ip, vm_ip, t_ip in wld_hosts:
            c.add_card(GlassCard(
                f"card-{fqdn}", x_pos, 430, 320, 235, f"{fqdn}.site-a.vcf.lab", "ESXi 8.0 U3", "🖥️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN,
                [
                    "Specs: 32 Cores | 128 GB RAM",
                    f"MGMT vmk0: {m_ip}",
                    f"vSAN vmk1: {v_ip}",
                    f"vMotion vmk2: {vm_ip}",
                    f"GENEVE TEP vmk50: {t_ip}",
                    "DS: vsan-cluster-wld01-01a"
                ],
                GlassmorphismCanvas.COLOR_AMBER
            ))
            x_pos += 340

        return c

    def build_nsx_architecture(self) -> GlassmorphismCanvas:
        """5. NSX-T Overlay & Edge Routing Topology"""
        c = GlassmorphismCanvas(
            width=1080, height=720,
            title="NSX-T Virtualization & Overlay Topology",
            subtitle="NSX Managers, Transport Nodes, Edge Clusters & GENEVE TEP Tunnels"
        )
        c.add_legend([
            ("Management NSX", GlassmorphismCanvas.COLOR_PURPLE),
            ("Workload NSX", GlassmorphismCanvas.COLOR_AMBER),
            ("GENEVE TEP Overlay", GlassmorphismCanvas.COLOR_ORANGE),
        ])
        
        # Mgmt NSX Container
        c.add_container(40, 85, 485, 600, "Management Domain NSX Fabric", subtitle="nsx-mgmt-01a", icon="🔀", border_color="rgba(188,140,255,0.3)")
        c.add_card(GlassCard("nsx-mgr-1", 65, 125, 435, 95, "NSX Management Cluster VIP", "nsx-mgmt-01a.site-a.vcf.lab", "🎛️", "VIP .11", GlassmorphismCanvas.COLOR_GREEN, ["Management Plane & Central Policy", "Cluster Nodes: nsx-mgmt-01a"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("tn-mgmt-c", 65, 240, 435, 110, "Host Transport Nodes (esx-01a..04a)", "N-VDS / VDS Integration", "🖥️", "4 NODES", GlassmorphismCanvas.COLOR_GREEN, ["Overlay TEP IPs: 10.1.5.131 - 10.1.5.134", "Transport Zone: tz-overlay-mgmt", "Uplink Profile: vcf-mgmt-uplink-profile"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("edge-mgmt-c", 65, 370, 435, 125, "Edge Cluster: edge-cluster-mgmt", "2 Edge VMs (nsx-edge-01a, 02a)", "🛡️", "ACTIVE/STDBY", GlassmorphismCanvas.COLOR_GREEN, ["Edge TEP IPs: 10.1.5.141, 10.1.5.142", "Tier-0 Gateway: t0-mgmt-gw", "Tier-1 Gateway: t1-mgmt-gw"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("t0-mgmt-card", 65, 515, 435, 140, "Tier-0 / Tier-1 Gateway Fabric", "Logical Routing & BGP Uplink", "🌐", "BGP ESTABLISHED", GlassmorphismCanvas.COLOR_GREEN, ["BGP Neighbor: holorouter (10.1.10.129)", "Active Uplinks: VLAN 101, VLAN 102", "NAT Rules: Outbound Internet Access"], GlassmorphismCanvas.COLOR_PURPLE))

        # Wld NSX Container
        c.add_container(555, 85, 485, 600, "Workload Domain NSX Fabric", subtitle="nsx-wld01-01a", icon="⚡", border_color="rgba(210,153,34,0.3)")
        c.add_card(GlassCard("nsx-mgr-2", 580, 125, 435, 95, "NSX Workload Cluster VIP", "nsx-wld01-01a.site-a.vcf.lab", "🎛️", "VIP .21", GlassmorphismCanvas.COLOR_GREEN, ["Tenant Overlay & Micro-segmentation", "Cluster Nodes: nsx-wld01-01a"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("tn-wld-c", 580, 240, 435, 110, "Host Transport Nodes (esx-05a..07a)", "VDS Integration", "🖥️", "3 NODES", GlassmorphismCanvas.COLOR_GREEN, ["Overlay TEP IPs: 10.1.5.135 - 10.1.5.137", "Transport Zone: tz-overlay-wld01", "Uplink Profile: vcf-wld-uplink-profile"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("edge-wld-c", 580, 370, 435, 125, "Edge Cluster: edge-cluster-wld01", "2 Edge VMs (nsx-wld-edge-01a, 02a)", "🛡️", "ACTIVE/STDBY", GlassmorphismCanvas.COLOR_GREEN, ["Edge TEP IPs: 10.1.5.143, 10.1.5.144", "Tier-0 Gateway: t0-wld01-gw", "Tier-1 Gateway: t1-wld01-gw"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("t0-wld-card", 580, 515, 435, 140, "Supervisor & Pod Overlay Fabric", "Container Network Interface (CNI)", "☸️", "GENEVE ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Spherelet & Kube-Proxy Integration", "Segment: seg-tkg-workload", "Load Balancer: Avi / NSX Advanced Load Balancer"], GlassmorphismCanvas.COLOR_AMBER))

        # Flow Edges
        c.add_edge(FlowEdge((280, 220), (280, 240), "Policy Sync", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((280, 350), (280, 370), "GENEVE TEP", GlassmorphismCanvas.COLOR_ORANGE))
        c.add_edge(FlowEdge((280, 495), (280, 515), "Routing", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((797, 220), (797, 240), "Policy Sync", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((797, 350), (797, 370), "GENEVE TEP", GlassmorphismCanvas.COLOR_ORANGE))
        c.add_edge(FlowEdge((797, 495), (797, 515), "CNI Routing", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((500, 430), (580, 430), "Inter-Edge TEP Tunnel", GlassmorphismCanvas.COLOR_ORANGE))

        return c

    def build_lab_boot_sequence(self) -> GlassmorphismCanvas:
        """6. Lab Startup & Service Boot Flow"""
        c = GlassmorphismCanvas(
            width=1100, height=720,
            title="Lab Startup Boot & Service Initialization Flow",
            subtitle="Orchestrated Startup Dependency Map (labstartup.py)"
        )
        c.add_legend([
            ("Phase 1: Core", GlassmorphismCanvas.COLOR_BLUE),
            ("Phase 2: Platform", GlassmorphismCanvas.COLOR_PURPLE),
            ("Phase 3: VCF Control", GlassmorphismCanvas.COLOR_AMBER),
            ("Phase 4: Operations", GlassmorphismCanvas.COLOR_GREEN),
        ])
        
        # Cards for Boot Steps arranged in 3x3 Grid Flow
        # Row 1
        c.add_card(GlassCard("boot-1", 50, 110, 290, 130, "Step 1: holorouter", "10.1.10.129", "🛡️", "STAGE 1", GlassmorphismCanvas.COLOR_GREEN, ["• Initialize DNS & DHCP", "• Start Squid Proxy (:3128)", "• Set up NAT & Firewall"], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("boot-2", 405, 110, 290, 130, "Step 2: manager VM", "10.1.10.131", "🚀", "STAGE 2", GlassmorphismCanvas.COLOR_GREEN, ["• Init lsfunctions runtime", "• Mount NFS exports", "• Read /tmp/config.ini"], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("boot-3", 760, 110, 290, 130, "Step 3: ESXi Hosts", "esx-01a .. esx-07a", "🖥️", "STAGE 3", GlassmorphismCanvas.COLOR_GREEN, ["• Verify SSH management", "• Exit Maintenance Mode", "• Check host power states"], GlassmorphismCanvas.COLOR_BLUE))
        
        # Row 2
        c.add_card(GlassCard("boot-6", 50, 295, 290, 130, "Step 6: vCenter Servers", "vc-mgmt-a & vc-wld01-a", "🏢", "STAGE 6", GlassmorphismCanvas.COLOR_GREEN, ["• Power on vCenter VMs", "• Poll VAMI API (:5480)", "• Verify SSO session tokens"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("boot-5", 405, 295, 290, 130, "Step 5: NSX Manager & Edges", "nsx-mgmt-01a & Edges", "🔀", "STAGE 5", GlassmorphismCanvas.COLOR_GREEN, ["• Power on NSX Cluster", "• Boot Edge Node VMs", "• Wait 5m for TEP sync"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("boot-4", 760, 295, 290, 130, "Step 4: vSAN Storage", "vsan-cluster-mgmt-01a", "💾", "STAGE 4", GlassmorphismCanvas.COLOR_GREEN, ["• Verify vSAN health", "• Mount vSAN Datastores", "• Check disk claim status"], GlassmorphismCanvas.COLOR_PURPLE))

        # Row 3
        c.add_card(GlassCard("boot-7", 50, 480, 290, 130, "Step 7: SDDC Manager", "sddcmanager-a", "🎛️", "STAGE 7", GlassmorphismCanvas.COLOR_GREEN, ["• Power on sddcmanager-a", "• Verify API access token", "• Audit domain health"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("boot-8", 405, 480, 290, 130, "Step 8: VCF Operations", "Aria & VCF Automation", "📊", "STAGE 8", GlassmorphismCanvas.COLOR_GREEN, ["• Boot VCF Ops Suite", "• Run URL checker pass", "• Run vcf-lab-tuner pass"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("boot-9", 760, 480, 290, 130, "Step 9: Lab Ready!", "System Fully Operational", "🎉", "COMPLETE", GlassmorphismCanvas.COLOR_GREEN, ["• Write startup_status.txt", "• Update status dashboard", "• Signal console ready"], GlassmorphismCanvas.COLOR_GREEN))

        # Flow Edges connecting stages
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
        """7. Core Infrastructure & L1 Management Services Topology"""
        c = GlassmorphismCanvas(
            width=1120, height=660,
            title="Core Infrastructure & Services Fabric (Layer 1)",
            subtitle="L1 Management, Security, Routing, DNS/DHCP, Proxy & Lab Automation Services"
        )
        c.add_legend([
            ("Network & Security", GlassmorphismCanvas.COLOR_BLUE),
            ("User Console & UI", GlassmorphismCanvas.COLOR_GREEN),
            ("Automation Engine", GlassmorphismCanvas.COLOR_PURPLE),
            ("External / Ingress", GlassmorphismCanvas.COLOR_MUTED),
        ])
        
        # Containers
        c.add_container(40, 85, 230, 535, "External Network", subtitle="192.168.0.0/24", icon="🌐", border_color="rgba(139,148,158,0.3)")
        c.add_container(305, 85, 490, 535, "Core Services Fabric (L1)", subtitle="10.1.10.128/25", icon="🛠️", border_color="rgba(88,166,255,0.3)")
        c.add_container(825, 85, 255, 535, "VCF L2 Ingress", subtitle="10.1.1.0/24", icon="☁️", border_color="rgba(188,140,255,0.3)")
        
        # Nodes - External
        c.add_card(GlassCard("ext-gateway", 65, 130, 180, 110, "External Gateway", "192.168.0.1", "🌐", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["Internet Connectivity", "vPod Host Ingress", "Upstream NAT"], GlassmorphismCanvas.COLOR_MUTED))
        c.add_card(GlassCard("ext-dns", 65, 275, 180, 100, "Upstream DNS", "10.1.10.129", "🔍", "READY", GlassmorphismCanvas.COLOR_GREEN, ["Technitium DNS Zone", "vcf.lab Resolvers"], GlassmorphismCanvas.COLOR_MUTED))
        c.add_card(GlassCard("ext-squid", 65, 410, 180, 100, "Squid Proxy Uplink", ":3128", "🛡️", "FILTERING", GlassmorphismCanvas.COLOR_GREEN, ["HTTP/HTTPS Proxy", "Lab Whitelist ACLs"], GlassmorphismCanvas.COLOR_MUTED))
        
        # Nodes - Core
        r_ip = self.env.router_ip or "10.1.10.129"
        con_ip = self.env.console_ip or "10.1.10.130"
        mgr_ip = self.env.manager_ip or "10.1.10.131"
        
        c.add_card(GlassCard("holorouter", 330, 130, 440, 145, "holorouter (Core Gateway & Security VM)", r_ip, "🛡️", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, [
            "• Technitium DNS & DHCP Server (Core / Mgmt / TEP Scopes)",
            "• Squid Caching & Filtering Proxy (:3128)",
            "• IPTables Firewall, NAT & Inter-VLAN Routing",
            "• Chrony NTP Server (:123)"
        ], GlassmorphismCanvas.COLOR_BLUE))
        
        c.add_card(GlassCard("console", 330, 305, 210, 175, "console (Linux Desktop)", con_ip, "🖥️", "READY", GlassmorphismCanvas.COLOR_GREEN, [
            "• Ubuntu Desktop GUI",
            "• Firefox (80% Default Zoom)",
            "• VNC Server (:5901)",
            "• RDP Server (:3389)",
            "• SSH Client Terminal"
        ], GlassmorphismCanvas.COLOR_GREEN))
        
        c.add_card(GlassCard("manager", 560, 305, 210, 175, "manager (Automation)", mgr_ip, "🚀", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, [
            "• labstartup.py Engine",
            "• lsfunctions Runtime",
            "• NFS Export (/tmp/holorouter)",
            "• pyVmomi / REST Automations",
            "• SSH Admin (:22 / :5480)"
        ], GlassmorphismCanvas.COLOR_PURPLE))
        
        c.add_card(GlassCard("core-subnet-info", 330, 505, 440, 90, "Core Interconnect & Automation Bus", "10.1.10.128/25 (VLAN 10)", "⚡", "10 GbE", GlassmorphismCanvas.COLOR_GREEN, [
            "Subnet: 10.1.10.128/25 | Gateway: 10.1.10.129 (holorouter)",
            "Static Allocation: .129 (Router), .130 (Console), .131 (Manager)"
        ], GlassmorphismCanvas.COLOR_BLUE))
        
        # Nodes - VCF Ingress
        c.add_card(GlassCard("vcf-sddc", 845, 130, 215, 100, "SDDC Manager", "10.1.1.5", "🎛️", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["VCF Fleet LCM", "REST API :443"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("vcf-vcenters", 845, 255, 215, 110, "vCenter Servers", "10.1.1.10 / 10.1.1.11", "🏢", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["vc-mgmt-a & vc-wld01-a", "pyVmomi & REST APIs"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("vcf-nsx", 845, 390, 215, 100, "NSX Managers", "10.1.1.21 / 10.1.1.25", "🔀", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Policy & Management", "VIP Clustering"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("vcf-ops", 845, 510, 215, 85, "VCF Operations", "10.1.1.30", "📊", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Aria Suite / Operations"], GlassmorphismCanvas.COLOR_PURPLE))
        
        # Edges
        c.add_edge(FlowEdge((245, 185), (330, 185), "NAT / Transit", GlassmorphismCanvas.COLOR_MUTED))
        c.add_edge(FlowEdge((550, 275), (435, 305), "DNS / DHCP", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((550, 275), (665, 305), "Routing", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((540, 392), (560, 392), "SSH / Control", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((770, 392), (845, 180), "API Orchestration", GlassmorphismCanvas.COLOR_PURPLE, waypoints=[(805, 392), (805, 180)]))
        c.add_edge(FlowEdge((770, 392), (845, 310), "vSphere API", GlassmorphismCanvas.COLOR_PURPLE, waypoints=[(805, 392), (805, 310)]))
        c.add_edge(FlowEdge((770, 392), (845, 440), "NSX API", GlassmorphismCanvas.COLOR_PURPLE, waypoints=[(805, 392), (805, 440)]))
        
        return c

    def build_dvs_topology(self) -> GlassmorphismCanvas:
        """8. Distributed Virtual Switch (DVS) & Port Group Topology"""
        c = GlassmorphismCanvas(
            width=1160, height=780,
            title="Distributed Virtual Switch (VDS) & Port Group Topology",
            subtitle="Virtual Networking Fabric across Management & Workload vCenter Instances"
        )
        c.add_legend([
            ("Management DVS", GlassmorphismCanvas.COLOR_PURPLE),
            ("Workload DVS", GlassmorphismCanvas.COLOR_AMBER),
            ("Storage / Infrastructure PG", GlassmorphismCanvas.COLOR_GREEN),
            ("App & Tanzu CNI PG", GlassmorphismCanvas.COLOR_CYAN),
        ])
        
        # Container Mgmt vCenter DVS
        c.add_container(35, 85, 530, 640, "Management vCenter: vc-mgmt-a.site-a.vcf.lab", subtitle="2 Distributed Switches (vds01 & vds02)", icon="🏢", border_color="rgba(188,140,255,0.3)")
        
        # Sub-container vds01-mgmt
        c.add_container(55, 125, 490, 270, "DVS: vds01-mgmt-01a (System & Overlay)", subtitle="Uplinks: vds01-mgmt-01a-DVUplinks-19", icon="🔀", border_color="rgba(188,140,255,0.2)")
        c.add_card(GlassCard("pg-mgmt-vds", 75, 165, 215, 95, "mgmt-vds01-mgmt-01a", "VLAN: Default / 101", "🌐", "SYSTEM", GlassmorphismCanvas.COLOR_GREEN, ["ESXi vmk0 Management", "vCenter / SDDC / NSX IPs"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("pg-vmmgmt", 310, 165, 215, 95, "vmmgmt-vds01-mgmt-01a", "VM Network", "🖥️", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Management VM Workloads", "Aria / VCF Ops Appliances"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("pg-vmotion-mgmt", 75, 280, 450, 95, "vmotion-vds-mgmt-01a", "VLAN: 103 (10.1.3.0/24)", "🔄", "HIGH SPEED", GlassmorphismCanvas.COLOR_GREEN, ["Live VM State & vMotion Migration Fabric", "Kernel Interface: vmk2 (10.1.3.101 - 104)"], GlassmorphismCanvas.COLOR_CYAN))
        
        # Sub-container vds02-mgmt
        c.add_container(55, 415, 490, 290, "DVS: vds02-mgmt-01a (vSAN Storage)", subtitle="Uplinks: vds02-mgmt-01a-DVUplinks-21", icon="💾", border_color="rgba(63,185,80,0.2)")
        c.add_card(GlassCard("pg-vsan-mgmt", 75, 455, 450, 110, "vsan-vds02-mgmt-01a", "VLAN: 102 (10.1.2.0/24)", "💾", "NVMe / SSD", GlassmorphismCanvas.COLOR_GREEN, ["Kernel Interface: vmk1 (10.1.2.101 - 104)", "vSAN ESA / OSA Clustered Datastore Fabric", "Jumbo Frames MTU 9000 Supported"], GlassmorphismCanvas.COLOR_GREEN))
        c.add_card(GlassCard("mgmt-dvs-uplink", 75, 580, 450, 105, "Physical Uplink Association (Mgmt Cluster)", "esx-01a .. esx-04a (vmnic0, vmnic1, vmnic2, vmnic3)", "🔌", "4x 10GbE", GlassmorphismCanvas.COLOR_GREEN, ["vmnic0/1 -> vds01-mgmt-01a (Mgmt/vMotion/Overlay)", "vmnic2/3 -> vds02-mgmt-01a (vSAN Storage Fabric)"], GlassmorphismCanvas.COLOR_BLUE))
        
        # Container Wld vCenter DVS
        c.add_container(595, 85, 530, 640, "Workload vCenter: vc-wld01-a.site-a.vcf.lab", subtitle="2 Distributed Switches (vds01 & vds02)", icon="🏬", border_color="rgba(210,153,34,0.3)")
        
        # Sub-container vds01-wld
        c.add_container(615, 125, 490, 270, "DVS: vds01-wld01-01a (Workload & Tanzu)", subtitle="Uplinks: vds01-wld01-01a-DVUplinks", icon="☸️", border_color="rgba(210,153,34,0.2)")
        c.add_card(GlassCard("pg-mgmt-wld", 635, 165, 215, 95, "mgmt-vds01-wld01-01a", "VLAN: 101", "🌐", "SYSTEM", GlassmorphismCanvas.COLOR_GREEN, ["ESXi vmk0 Management", "Supervisor Control Plane"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("pg-services-wld", 870, 165, 215, 95, "Services Subnet", "Tanzu Ingress", "⚡", "ROUTED", GlassmorphismCanvas.COLOR_GREEN, ["TKG Cluster Services", "Load Balancer VIPs"], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("pg-apps-wld", 635, 280, 450, 95, "Tanzu Pod & App Port Groups", "pod-default | bookstore-app | kubernetes-cluster", "☸️", "DYNAMIC", GlassmorphismCanvas.COLOR_GREEN, ["Antrea CNI / NSX Container Plugin Segments", "Microservices & Database Workload Attachments"], GlassmorphismCanvas.COLOR_CYAN))
        
        # Sub-container vds02-wld
        c.add_container(615, 415, 490, 290, "DVS: vds02-wld01-01a (vSAN Storage)", subtitle="Uplinks: vds02-wld01-01a-DVUplinks-11", icon="💾", border_color="rgba(63,185,80,0.2)")
        c.add_card(GlassCard("pg-vsan-wld", 635, 455, 450, 110, "vsan-vds02-wld01-01a", "VLAN: 102 (10.1.2.0/24)", "💾", "NVMe / SSD", GlassmorphismCanvas.COLOR_GREEN, ["Kernel Interface: vmk1 (10.1.2.105 - 107)", "vSAN Clustered Storage for Tanzu PVCs", "Storage Policy: Tanzu Workload Default"], GlassmorphismCanvas.COLOR_GREEN))
        c.add_card(GlassCard("wld-dvs-uplink", 635, 580, 450, 105, "Physical Uplink Association (Wld Cluster)", "esx-05a .. esx-07a (vmnic0, vmnic1, vmnic2, vmnic3)", "🔌", "4x 10GbE", GlassmorphismCanvas.COLOR_GREEN, ["vmnic0/1 -> vds01-wld01-01a (Workload/Overlay/CNI)", "vmnic2/3 -> vds02-wld01-01a (vSAN Storage Fabric)"], GlassmorphismCanvas.COLOR_AMBER))
        
        return c

    def build_storage_summary(self) -> GlassmorphismCanvas:
        """9. vSAN Clustered Storage & Datastore Architecture"""
        c = GlassmorphismCanvas(
            width=1120, height=700,
            title="vSAN Clustered Storage & Capacity Architecture",
            subtitle="Enterprise vSAN Capacity Fabric, Datastore Allocation & Resiliency Policies"
        )
        c.add_legend([
            ("Management Storage", GlassmorphismCanvas.COLOR_PURPLE),
            ("Workload Storage", GlassmorphismCanvas.COLOR_AMBER),
            ("Storage Policy", GlassmorphismCanvas.COLOR_CYAN),
            ("Health & Status", GlassmorphismCanvas.COLOR_GREEN),
        ])
        
        # Calculate summary statistics from self.env.datastores
        ds_mgmt_cap, ds_mgmt_free, ds_wld_cap, ds_wld_free = 4799.5, 3248.1, 2699.8, 2148.2
        for ds in self.env.datastores:
            if 'mgmt' in ds.name.lower():
                ds_mgmt_cap = ds.capacity_gb
                ds_mgmt_free = ds.free_gb
            elif 'wld' in ds.name.lower():
                ds_wld_cap = ds.capacity_gb
                ds_wld_free = ds.free_gb
        
        total_cap = ds_mgmt_cap + ds_wld_cap
        total_free = ds_mgmt_free + ds_wld_free
        total_used = total_cap - total_free
        used_pct = (total_used / total_cap * 100) if total_cap > 0 else 0
        
        # Header Overview Cards (3 columns)
        c.add_card(GlassCard("st-total", 50, 90, 310, 100, "Total vSAN Storage Pool", f"{total_cap:.1f} GB ({total_cap/1024:.2f} TB)", "💾", "AGGREGATE", GlassmorphismCanvas.COLOR_GREEN, [f"Used: {total_used:.1f} GB ({used_pct:.0f}%)", f"Free: {total_free:.1f} GB"], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("st-mgmt-ov", 405, 90, 310, 100, "Management Datastore", f"{ds_mgmt_cap:.1f} GB", "🏛️", "vSAN Flash", GlassmorphismCanvas.COLOR_GREEN, [f"Free: {ds_mgmt_free:.1f} GB ({(ds_mgmt_cap-ds_mgmt_free)/ds_mgmt_cap*100:.0f}% used)", "Cluster: cluster-mgmt-01a"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("st-wld-ov", 760, 90, 310, 100, "Workload Datastore", f"{ds_wld_cap:.1f} GB", "⚡", "vSAN Flash", GlassmorphismCanvas.COLOR_GREEN, [f"Free: {ds_wld_free:.1f} GB ({(ds_wld_cap-ds_wld_free)/ds_wld_cap*100:.0f}% used)", "Cluster: cluster-wld01-01a"], GlassmorphismCanvas.COLOR_AMBER))
        
        # Container Mgmt Datastore
        c.add_container(50, 225, 485, 430, "Management Datastore: vsan-mgmt-01a", subtitle="Cluster: cluster-mgmt-01a (4 Hosts)", icon="💾", border_color="rgba(188,140,255,0.3)")
        c.add_card(GlassCard("ds-mgmt-info", 75, 270, 435, 110, "Capacity & Space Allocation", f"Capacity: {ds_mgmt_cap:.1f} GB | Free: {ds_mgmt_free:.1f} GB", "📊", f"{(ds_mgmt_cap-ds_mgmt_free)/ds_mgmt_cap*100:.0f}% USED", GlassmorphismCanvas.COLOR_GREEN, [
            "• Type: VMware vSAN All-Flash Cluster",
            "• Deduplication & Compression: Enabled",
            "• Free Capacity: ~" + f"{ds_mgmt_free/1024:.2f} TB available"
        ], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("ds-mgmt-policy", 75, 395, 435, 110, "Resiliency Policy & Protection", "FTT=1 (RAID-1 Mirroring)", "🛡️", "COMPLIANT", GlassmorphismCanvas.COLOR_GREEN, [
            "• Failures to Tolerate: 1 Host/Disk Failure",
            "• Disk Object Stripe Width: 1",
            "• Storage Policy: VCF Management Default"
        ], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("ds-mgmt-consumers", 75, 520, 435, 110, "Associated Workloads & Consumers", "4 Hosts (esx-01a .. esx-04a)", "🖥️", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, [
            "• SDDC Manager, Management vCenter, NSX Manager",
            "• Aria Suite / VCF Operations, Automation & Logs",
            "• Network: 10.1.2.101 - 10.1.2.104 (vmk1 vSAN Fabric)"
        ], GlassmorphismCanvas.COLOR_PURPLE))
        
        # Container Wld Datastore
        c.add_container(585, 225, 485, 430, "Workload Datastore: vsan-wld01-01a", subtitle="Cluster: cluster-wld01-01a (3 Hosts)", icon="💾", border_color="rgba(210,153,34,0.3)")
        c.add_card(GlassCard("ds-wld-info", 610, 270, 435, 110, "Capacity & Space Allocation", f"Capacity: {ds_wld_cap:.1f} GB | Free: {ds_wld_free:.1f} GB", "📊", f"{(ds_wld_cap-ds_wld_free)/ds_wld_cap*100:.0f}% USED", GlassmorphismCanvas.COLOR_GREEN, [
            "• Type: VMware vSAN All-Flash Cluster",
            "• Persistent Volume Storage for Tanzu & K8s",
            "• Free Capacity: ~" + f"{ds_wld_free/1024:.2f} TB available"
        ], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("ds-wld-policy", 610, 395, 435, 110, "Resiliency Policy & Protection", "FTT=1 (RAID-1 Mirroring)", "🛡️", "COMPLIANT", GlassmorphismCanvas.COLOR_GREEN, [
            "• Failures to Tolerate: 1 Host/Disk Failure",
            "• Tanzu Cloud Native Storage (CNS / CSI) Binding",
            "• Storage Policy: Tanzu Workload Default"
        ], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("ds-wld-consumers", 610, 520, 435, 110, "Associated Workloads & Consumers", "3 Hosts (esx-05a .. esx-07a)", "🖥️", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, [
            "• Supervisor Control Plane, Tanzu K8s Clusters",
            "• Harbor Registry, DSM, PostgreSQL, Acme Pods",
            "• Network: 10.1.2.105 - 10.1.2.107 (vmk1 vSAN Fabric)"
        ], GlassmorphismCanvas.COLOR_AMBER))
        
        return c

    def build_complete_infrastructure(self) -> GlassmorphismCanvas:
        """10. Complete VCF Lab Holistic Multi-Tier Infrastructure Topology"""
        c = GlassmorphismCanvas(
            width=1200, height=880,
            title="Complete VCF Lab Infrastructure Topology",
            subtitle="Multi-Tier Physical & Virtual Topology across External, Layer 1 Core & Layer 2 VCF Stack"
        )
        c.add_legend([
            ("External / Ingress", GlassmorphismCanvas.COLOR_MUTED),
            ("L1 Core Services", GlassmorphismCanvas.COLOR_BLUE),
            ("Management Control Plane", GlassmorphismCanvas.COLOR_PURPLE),
            ("Workload & Container Fabric", GlassmorphismCanvas.COLOR_AMBER),
            ("Operations & Automation Suite", GlassmorphismCanvas.COLOR_CYAN),
        ])
        
        # Layer 0: External Access
        c.add_container(40, 80, 1120, 95, "Layer 0: External Access & Upstream Network", subtitle="192.168.0.0/24", icon="🌐", border_color="rgba(139,148,158,0.3)")
        c.add_card(GlassCard("c-ext-gw", 70, 105, 330, 60, "Internet Gateway", "192.168.0.1", "🌐", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["Default Upstream Route"], GlassmorphismCanvas.COLOR_MUTED))
        c.add_card(GlassCard("c-ext-dns", 435, 105, 330, 60, "Technitium DNS Resolver", "10.1.10.129", "🔍", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Authoritative DNS: vcf.lab"], GlassmorphismCanvas.COLOR_MUTED))
        c.add_card(GlassCard("c-ext-squid", 800, 105, 330, 60, "Squid Proxy Gateway", "10.1.10.129:3128", "🛡️", "FILTERING", GlassmorphismCanvas.COLOR_GREEN, ["Web Ingress & Whitelist"], GlassmorphismCanvas.COLOR_MUTED))
        
        # Layer 1: Core VMs
        c.add_container(40, 195, 1120, 110, "Layer 1: Core Infrastructure VMs (L1 Fabric)", subtitle="10.1.10.128/25", icon="🛠️", border_color="rgba(88,166,255,0.3)")
        r_ip = self.env.router_ip or "10.1.10.129"
        con_ip = self.env.console_ip or "10.1.10.130"
        mgr_ip = self.env.manager_ip or "10.1.10.131"
        c.add_card(GlassCard("c-router", 70, 225, 330, 68, "holorouter (Router / FW / DNS)", r_ip, "🛡️", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["DNS/DHCP, Squid Proxy, NAT/FW"], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("c-console", 435, 225, 330, 68, "console (Linux Desktop)", con_ip, "🖥️", "READY", GlassmorphismCanvas.COLOR_GREEN, ["Ubuntu Desktop, Firefox 80%, VNC"], GlassmorphismCanvas.COLOR_BLUE))
        c.add_card(GlassCard("c-manager", 800, 225, 330, 68, "manager (Lab Automation Engine)", mgr_ip, "🚀", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["labstartup.py, Python lsfunctions"], GlassmorphismCanvas.COLOR_BLUE))
        
        # Layer 2: VCF Management Domain
        c.add_container(40, 325, 545, 370, "Layer 2: Management Domain (mgmt-a)", subtitle="10.1.1.0/24", icon="🏛️", border_color="rgba(188,140,255,0.3)")
        c.add_card(GlassCard("c-sddc", 65, 360, 235, 80, "SDDC Manager", "sddcmanager-a (10.1.1.5)", "🎛️", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["VCF Fleet LCM", "REST API :443"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("c-vcmgmt", 325, 360, 235, 80, "vCenter Server", "vc-mgmt-a (10.1.1.10)", "🏢", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["SSO: vsphere.local", "VAMI :5480"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("c-nsxmgmt", 65, 455, 235, 80, "NSX Manager", "nsx-mgmt-01a (10.1.1.21)", "🔀", "HA READY", GlassmorphismCanvas.COLOR_GREEN, ["Policy & Overlay", "VIP Management"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("c-mgmthosts", 325, 455, 235, 80, "Mgmt ESXi Cluster", "4 Hosts (esx-01a..04a)", "🖥️", "4/4 UP", GlassmorphismCanvas.COLOR_GREEN, ["128 Cores | 512 GB", "10.1.1.101 - 104"], GlassmorphismCanvas.COLOR_PURPLE))
        c.add_card(GlassCard("c-vsanmgmt", 65, 550, 495, 80, "vSAN Datastore: vsan-mgmt-01a", "Capacity: ~4.8 TB | FTT=1 Mirroring", "💾", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, ["vSAN ESA/OSA Storage Fabric for Management VMs"], GlassmorphismCanvas.COLOR_PURPLE))
        
        # Layer 2: VCF Workload Domain
        c.add_container(615, 325, 545, 370, "Layer 2: Workload Domain (wld01-a)", subtitle="10.1.1.0/24", icon="⚡", border_color="rgba(210,153,34,0.3)")
        c.add_card(GlassCard("c-vcwld", 640, 360, 235, 80, "vCenter Workload", "vc-wld01-a (10.1.1.11)", "🏬", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["SSO: wld.sso", "VAMI :5480"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("c-nsxwld", 900, 360, 235, 80, "NSX Workload", "nsx-wld01-01a (10.1.1.25)", "🔀", "HA READY", GlassmorphismCanvas.COLOR_GREEN, ["Tenant Overlay & CNI", "VIP Management"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("c-wldhosts", 640, 455, 235, 80, "Workload ESXi Cluster", "3 Hosts (esx-05a..07a)", "🖥️", "3/3 UP", GlassmorphismCanvas.COLOR_GREEN, ["96 Cores | 384 GB", "10.1.1.105 - 107"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("c-scp", 900, 455, 235, 80, "Supervisor CP & Tanzu", "SupervisorControlPlaneVM", "☸️", "RUNNING", GlassmorphismCanvas.COLOR_GREEN, ["Spherelet & K8s VIP", "Bookstore & DSM Apps"], GlassmorphismCanvas.COLOR_AMBER))
        c.add_card(GlassCard("c-vsanwld", 640, 550, 495, 80, "vSAN Datastore: vsan-wld01-01a", "Capacity: ~2.7 TB | FTT=1 Mirroring", "💾", "HEALTHY", GlassmorphismCanvas.COLOR_GREEN, ["vSAN Workload Storage Fabric for Tanzu PVCs"], GlassmorphismCanvas.COLOR_AMBER))
        
        # Layer 2: VCF Operations & Automation Suite
        c.add_container(40, 715, 1120, 140, "Layer 2: VCF Operations Suite & Automation Appliances", subtitle="Management & Analytics Plane", icon="📊", border_color="rgba(56,189,248,0.3)")
        c.add_card(GlassCard("c-ops-a", 70, 750, 255, 85, "VCF Operations (Aria)", "ops-a.site-a.vcf.lab (10.1.1.30)", "📊", "RUNNING", GlassmorphismCanvas.COLOR_GREEN, ["Telemetry, Analytics & Dashboards"], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("c-auto", 355, 750, 255, 85, "VCF Automation Platform", "auto-platform-a (10.1.1.73)", "⚡", "RUNNING", GlassmorphismCanvas.COLOR_GREEN, ["Microservices & Cloud Templates"], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("c-opscoll", 640, 750, 240, 85, "Ops Collector & Networks", "opscollector / opsnet (10.1.1.41/60)", "📡", "ACTIVE", GlassmorphismCanvas.COLOR_GREEN, ["Network & Metric Ingestion"], GlassmorphismCanvas.COLOR_CYAN))
        c.add_card(GlassCard("c-support", 910, 750, 225, 85, "Services Runtime & Salt", "license-a / salt-a", "🔑", "ONLINE", GlassmorphismCanvas.COLOR_GREEN, ["License Server & Salt Config"], GlassmorphismCanvas.COLOR_CYAN))
        
        # Inter-Layer Glowing Flow Edges
        c.add_edge(FlowEdge((235, 165), (235, 225), "Ingress", GlassmorphismCanvas.COLOR_MUTED))
        c.add_edge(FlowEdge((400, 260), (435, 260), "DNS / DHCP", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((765, 260), (800, 260), "Control", GlassmorphismCanvas.COLOR_BLUE))
        c.add_edge(FlowEdge((965, 293), (200, 360), "VCF API", GlassmorphismCanvas.COLOR_PURPLE, waypoints=[(965, 310), (200, 310)]))
        c.add_edge(FlowEdge((965, 293), (760, 360), "Wld API", GlassmorphismCanvas.COLOR_AMBER, waypoints=[(965, 310), (760, 310)]))
        c.add_edge(FlowEdge((300, 400), (325, 400), "SDDC Control", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((875, 400), (900, 400), "SSO Federation", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((442, 535), (442, 550), "vSAN Fabric", GlassmorphismCanvas.COLOR_PURPLE))
        c.add_edge(FlowEdge((757, 535), (757, 550), "vSAN Fabric", GlassmorphismCanvas.COLOR_AMBER))
        c.add_edge(FlowEdge((442, 630), (200, 750), "Telemetry Ingestion", GlassmorphismCanvas.COLOR_CYAN, waypoints=[(442, 690), (200, 690)]))
        c.add_edge(FlowEdge((757, 630), (480, 750), "Automation Deploy", GlassmorphismCanvas.COLOR_CYAN, waypoints=[(757, 690), (480, 690)]))
        
        return c

    def build_all(self) -> Dict[str, str]:
        """Generate and return map of filename to SVG XML content (all 10 diagrams)"""
        return {
            "high_level_architecture.svg": self.build_high_level_architecture().render(),
            "network_dataflow.svg": self.build_network_dataflow().render(),
            "vcf_domain_architecture.svg": self.build_vcf_domain_architecture().render(),
            "esxi_host_layout.svg": self.build_esxi_host_layout().render(),
            "core_infrastructure.svg": self.build_core_infrastructure().render(),
            "dvs_topology.svg": self.build_dvs_topology().render(),
            "nsx_architecture.svg": self.build_nsx_architecture().render(),
            "lab_boot_sequence.svg": self.build_lab_boot_sequence().render(),
            "storage_summary.svg": self.build_storage_summary().render(),
            "complete_infrastructure.svg": self.build_complete_infrastructure().render(),
        }

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
            self.env.lab_type = self.config.get('VPOD', 'labtype').upper()
        
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
    """Generates LABDETAILS.md & HTML documentation from collected environment data"""
    
    def __init__(self, env: LabEnvironment, diagram_style: str = "glassmorphism", svg_rel_dir: str = "diagrams"):
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
        html.append(f'  <title>{sku} - Lab Environment Documentation v2.1</title>')
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
        html.append('      <a class="nav-link" href="#core-vms">Core VMs</a>')
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

        # 7. Core Infrastructure VMs
        add_diagram_card("core-vms", "🛠️", "Core Infrastructure & Services Fabric (Layer 1)", "core_infrastructure.svg", "L1 routing, Technitium DNS, DHCP, Squid proxy, desktop console, and manager automation.")

        # 8. Network Subnets Reference Table
        html.append('    <!-- Network Subnets Reference -->')
        html.append('    <div class="card" id="subnets">')
        html.append('      <h2><span>🌐</span> Network Subnets Reference</h2>')
        html.append('      <div class="table-wrap">')
        html.append('        <table>')
        html.append('          <thead><tr><th>Network</th><th>Subnet</th><th>Gateway</th><th>Purpose</th></tr></thead>')
        html.append('          <tbody>')
        html.append('            <tr><td><strong>Core / Services</strong></td><td><code>10.1.10.128/25</code></td><td><code>10.1.10.129</code></td><td>Console, Manager VM, Router, DNS/DHCP</td></tr>')
        html.append('            <tr><td><strong>VCF Management</strong></td><td><code>10.1.1.0/24</code></td><td><code>10.1.1.1</code></td><td>vCenter, SDDC Manager, NSX Manager, Aria Suite</td></tr>')
        html.append('            <tr><td><strong>vSAN Storage</strong></td><td><code>10.1.2.0/24</code></td><td>-</td><td>Dedicated Clustered vSAN Storage Fabric</td></tr>')
        html.append('            <tr><td><strong>vMotion Migration</strong></td><td><code>10.1.3.0/24</code></td><td>-</td><td>High-Speed Live VM State Migration</td></tr>')
        html.append('            <tr><td><strong>NSX GENEVE TEP</strong></td><td><code>10.1.5.128/25</code></td><td><code>10.1.5.129</code></td><td>NSX Overlay Transport Node &amp; Edge Tunnel Endpoints</td></tr>')
        html.append('            <tr><td><strong>External (Holodeck)</strong></td><td><code>192.168.0.0/24</code></td><td><code>192.168.0.1</code></td><td>vPod Host Uplink &amp; External Internet Access</td></tr>')
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
        add_diagram_card("complete", "🗺️", "Complete VCF Lab Holistic Infrastructure Topology", "complete_infrastructure.svg", "End-to-end multi-tier physical and virtual topology across External, Layer 1 Core, and Layer 2 VCF domains.")

        # 16. Quick Reference Commands
        html.append('    <!-- Quick Reference Commands -->')
        html.append('    <div class="card" id="quick-ref">')
        html.append('      <h2><span>⚡</span> Quick Reference Commands</h2>')
        
        # Snippet 1: Lab Startup
        html.append('      <h3>1. Lab Startup &amp; Health Dashboard (Bash)</h3>')
        html.append('      <div class="terminal">')
        html.append('        <div class="terminal-header"><div class="terminal-dots"><div class="dot dot-red"></div><div class="dot dot-yellow"></div><div class="dot dot-green"></div></div><div class="terminal-title">bash • lab startup</div></div>')
        html.append('        <pre class="terminal-body"><code># Full automated lab startup\ncd /home/holuser/hol &amp;&amp; python3 labstartup.py\n\n# Check lab readiness status\ncat /lmchol/startup_status.txt\n\n# View graphical startup status dashboard\nfirefox /lmchol/home/holuser/startup-status.htm</code></pre>')
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
        html.append('          <tr><td><strong>Generated By</strong></td><td><code>python3 Tools/generate_labdetails.py --html</code></td></tr>')
        html.append('          <tr><td><strong>Diagram Engine License</strong></td><td>MIT License © 2025 fireworks-tech-graph contributors</td></tr>')
        html.append('          <tr><td><strong>Lab Configuration</strong></td><td><code>/tmp/config.ini</code></td></tr>')
        html.append(f'          <tr><td><strong>Source INI</strong></td><td><code>/home/holuser/hol/holodeck/{sku}.ini</code></td></tr>')
        html.append('          <tr><td><strong>Lab Startup Script</strong></td><td><code>/home/holuser/hol/labstartup.py</code></td></tr>')
        html.append('        </table>')
        html.append('      </div>')
        html.append('    </div>')
        html.append('    ')
        html.append('    <div class="footer">')
        html.append('      <p>Generated by <strong>Tools/generate_labdetails.py</strong> v2.1 | Style 5 Glassmorphism Engine</p>')
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
        if self.diagram_style in ("glassmorphism", "both"):
            self._add(f"![High-Level Architecture]({self.svg_rel_dir}/high_level_architecture.svg)")
            self._add()
        if self.diagram_style in ("mermaid", "both"):
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
        if self.diagram_style in ("glassmorphism", "both"):
            self._add(f"![Multi-Plane Network & Data Flow Topology]({self.svg_rel_dir}/network_dataflow.svg)")
            self._add()
        if self.diagram_style in ("mermaid", "both"):
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
    
    def _add_core_infrastructure(self):
        """Add core infrastructure VMs diagram"""
        self._add("## Core Infrastructure VMs")
        self._add()
        if self.diagram_style in ("glassmorphism", "both"):
            self._add(f"![Core Infrastructure VMs]({self.svg_rel_dir}/core_infrastructure.svg)")
            self._add()
        if self.diagram_style in ("mermaid", "both"):
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
        self._add(f"| **Generator Version** | `v2.1` (Style 5 Glassmorphism Engine) |")
        self._add(f"| **Generated By** | `python3 Tools/generate_labdetails.py` |")
        self._add(f"| **Diagram Engine License** | MIT License © 2025 fireworks-tech-graph contributors |")
        self._add("| **Lab Configuration** | `/tmp/config.ini` |")
        self._add(f"| **Source INI** | `/home/holuser/hol/holodeck/{self.env.lab_sku}.ini` |")
        self._add("| **Lab Startup Script** | `/home/holuser/hol/labstartup.py` |")

#==============================================================================
# MAIN & CLI HELP SCREEN
#==============================================================================

VERSION = "2.1.0"

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
    print(cyan("║") + bold("  Automatic Lab Documentation & Style 5 Glassmorphism Topology Generator        ") + cyan("║"))
    print(cyan("╚════════════════════════════════════════════════════════════════════════════════╝"))
    print()
    print(bold("DESCRIPTION:"))
    print("  Queries live vCenter, NSX, and SDDC Manager environments to generate comprehensive")
    print("  LABDETAILS.md and LABDETAILS.html documentation along with 10 standalone Style 5")
    print("  Glassmorphism SVG topology diagrams illustrating connectivity and data flow across")
    print("  all 5 network planes.")
    print()
    print(bold("USAGE:"))
    print(f"  {green('python3 Tools/generate_labdetails.py')} [{yellow('[OPTIONS]')}]")
    print()
    print(bold("OPTIONS:"))
    print(f"  {green('-o, --output')} {yellow('<path>')}         Output markdown file path {dim(f'(default: {DEFAULT_OUTPUT})')}")
    print(f"  {green('--diagram-style')} {yellow('<style>')}   Diagram format style: {yellow('glassmorphism')}, {yellow('mermaid')}, or {yellow('both')} {dim('(default: glassmorphism)')}")
    print(f"  {green('--svg-dir')} {yellow('<path>')}         Directory for output SVG files {dim('(default: <output_dir>/diagrams)')}")
    print(f"  {green('--html')}                    Generate standalone glassmorphic viewer {yellow('LABDETAILS.html')}")
    print(f"  {green('--config')} {yellow('<path>')}         Path to config.ini {dim(f'(default: {CONFIG_INI})')}")
    print(f"  {green('--dry-run')}                  Print markdown to stdout without writing files")
    print(f"  {green('-v, --version')}              Display script version and exit")
    print(f"  {green('-h, --help')}                 Show this styled help screen and exit")
    print()
    print(bold("EXAMPLES:"))
    print(f"  {dim('# Generate standard LABDETAILS.md with Glassmorphism SVGs in diagrams/')}")
    print(f"  {green('python3 Tools/generate_labdetails.py')}")
    print()
    print(f"  {dim('# Generate both Glassmorphism SVGs and Mermaid code blocks along with HTML report')}")
    print(f"  {green('python3 Tools/generate_labdetails.py --diagram-style both --html')}")
    print()
    print(f"  {dim('# Dry-run generation to stdout')}")
    print(f"  {green('python3 Tools/generate_labdetails.py --dry-run')}")
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
        description='Generate LABDETAILS.md from live lab environment',
        add_help=False
    )
    parser.add_argument(
        '--output', '-o',
        default=DEFAULT_OUTPUT,
        help=f'Output file path (default: {DEFAULT_OUTPUT})'
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
        help='Directory path for generated SVGs'
    )
    parser.add_argument(
        '--html',
        action='store_true',
        help='Generate standalone LABDETAILS.html viewer'
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
    
    # Resolve output paths & directories
    output_path = os.path.abspath(args.output)
    output_dir = os.path.dirname(output_path)
    
    if args.svg_dir:
        svg_dir = os.path.abspath(args.svg_dir)
    else:
        svg_dir = os.path.join(output_dir, 'diagrams')
        
    # Calculate relative SVG directory for markdown links
    try:
        svg_rel_dir = os.path.relpath(svg_dir, output_dir)
    except Exception:
        svg_rel_dir = 'diagrams'
    
    # Check for creds.txt
    if not os.path.isfile(CREDS_FILE):
        print(f"WARNING: Credentials file not found: {CREDS_FILE}. Proceeding with offline fallback mode.")
    
    # Collect lab data
    collector = LabDataCollector(args.config)
    env = collector.collect_all()
    
    # Build Style 5 Glassmorphism Diagrams
    diagram_builder = LabDiagramBuilder(env)
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
        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(svg_dir, exist_ok=True)
        
        # Write SVGs to disk
        print(f"\nWriting Glassmorphism SVG diagrams to {svg_dir}...")
        for filename, svg_data in svg_map.items():
            svg_path = os.path.join(svg_dir, filename)
            with open(svg_path, 'w', encoding='utf-8') as f:
                f.write(svg_data)
            print(f"  ✓ {filename} ({len(svg_data.splitlines())} lines)")
            
        # Write Markdown file
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"\nLABDETAILS.md generated: {output_path}")
        print(f"Total lines: {len(content.splitlines())}")
        
        # Optionally generate HTML report
        if args.html:
            html_path = os.path.splitext(output_path)[0] + '.html'
            html_content = generator.generate_html(svg_map)
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            print(f"LABDETAILS.html generated: {html_path}")

if __name__ == '__main__':
    main()

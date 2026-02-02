#!/usr/bin/env python3
# status_dashboard.py - HOLFY27 Lab Startup Status Dashboard
# Version 1.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Generates an auto-refreshing HTML status page for lab startup monitoring

import os
import datetime
import json
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum

#==============================================================================
# CONFIGURATION
#==============================================================================

STATUS_FILE = '/lmchol/home/holuser/startup-status.htm'
STATE_FILE = '/tmp/startup-state.json'
REFRESH_SECONDS = 30

#==============================================================================
# STATUS TYPES
#==============================================================================

class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Task:
    id: str
    name: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    start_time: Optional[datetime.datetime] = None
    end_time: Optional[datetime.datetime] = None
    message: str = ""


@dataclass
class TaskGroup:
    id: str
    name: str
    tasks: List[Task] = field(default_factory=list)
    
    @property
    def status(self) -> TaskStatus:
        if not self.tasks:
            return TaskStatus.PENDING
        
        statuses = [t.status for t in self.tasks]
        
        if TaskStatus.FAILED in statuses:
            return TaskStatus.FAILED
        if TaskStatus.RUNNING in statuses:
            return TaskStatus.RUNNING
        if all(s == TaskStatus.COMPLETE for s in statuses):
            return TaskStatus.COMPLETE
        if all(s == TaskStatus.SKIPPED for s in statuses):
            return TaskStatus.SKIPPED
        return TaskStatus.PENDING
    
    @property
    def progress(self) -> float:
        if not self.tasks:
            return 0.0
        completed = sum(1 for t in self.tasks if t.status in [TaskStatus.COMPLETE, TaskStatus.SKIPPED])
        return (completed / len(self.tasks)) * 100


#==============================================================================
# STATUS DASHBOARD CLASS
#==============================================================================

class StatusDashboard:
    """Generate and update lab startup status HTML dashboard"""
    
    STATUS_ICONS = {
        TaskStatus.PENDING: ('⏳', '#6b7280', 'Pending - Waiting to start'),
        TaskStatus.RUNNING: ('🔄', '#3b82f6', 'Running - In progress'),
        TaskStatus.COMPLETE: ('✅', '#22c55e', 'Complete - Successfully finished'),
        TaskStatus.FAILED: ('❌', '#ef4444', 'Failed - Error occurred'),
        TaskStatus.SKIPPED: ('⏭️', '#8b5cf6', 'Skipped - Not required')
    }
    
    def __init__(self, lab_sku: str, load_state: bool = True):
        self.lab_sku = lab_sku
        self.start_time = datetime.datetime.now()
        self.groups: Dict[str, TaskGroup] = {}
        self.failed = False
        self.failure_reason = ""
        self._init_default_groups()
        
        # Try to load existing state to preserve progress across module calls
        if load_state:
            self._load_state()
    
    def _init_default_groups(self):
        """
        Initialize default task groups based on startup module sequence.
        
        Groups are ordered top-to-bottom matching the actual execution order
        from labstartup.py. Each group corresponds to a Startup/ module.
        
        Execution order:
        1. prelim.py    - Preliminary checks (DNS, README, firewall)
        2. ESXi.py      - ESXi host verification
        3. VCF.py       - VCF startup (management cluster, NSX, vCenter)
        4. VVF.py       - VVF startup (alternative to VCF)
        5. vSphere.py   - vSphere configuration (clusters, VMs)
        6. pings.py     - Network connectivity verification
        7. services.py  - Linux services and TCP port verification
        8. Kubernetes.py - Kubernetes certificate checks
        9. VCFfinal.py  - VCF final tasks (Tanzu, Aria)
        10. urls.py     - URL verification
        11. odyssey.py  - Odyssey client installation
        12. final.py    - Final checks and cleanup
        """
        # Define groups in execution order (top-to-bottom)
        default_groups = [
            # Group 1: prelim.py - Preliminary Checks
            ('prelim', '1. Preliminary Checks (prelim.py)', [
                ('readme', 'README Sync', 'Copy README to console desktop'),
                ('update_manager', 'Update Manager', 'Disable Ubuntu update popups'),
                ('dns', 'DNS Health Checks', 'Verify DNS resolution for all sites'),
                ('dns_import', 'DNS Record Import', 'Import custom DNS records'),
                ('firewall', 'Firewall Verification', 'Confirm firewall is active'),
                ('proxy_filter', 'Proxy Filter', 'Verify proxy filtering is active'),
            ]),
            
            # Group 2: ESXi.py - ESXi Host Verification
            ('esxi', '2. ESXi Host Verification (ESXi.py)', [
                ('host_check', 'Host Connectivity', 'Ping and verify ESXi hosts are responding'),
                ('host_ports', 'Host Port Checks', 'Verify ESXi management ports (443, 902)'),
            ]),
            
            # Group 3: VCF.py - VCF Startup (skipped for VVF labs)
            ('vcf', '3. VCF Startup (VCF.py)', [
                ('mgmt_cluster', 'Management Cluster', 'Connect to VCF management cluster hosts'),
                ('exit_maintenance', 'Exit Maintenance Mode', 'Remove hosts from maintenance mode'),
                ('datastore', 'Datastore Verification', 'Verify VCF management datastore'),
                ('nsx_mgr', 'NSX Manager', 'Start and verify NSX Manager VM'),
                ('nsx_edges', 'NSX Edge VMs', 'Start NSX Edge virtual machines'),
                ('vcenter', 'vCenter Server', 'Start and verify vCenter Server'),
            ]),
            
            # Group 4: VVF.py - VVF Startup (skipped for VCF labs)
            ('vvf', '4. VVF Startup (VVF.py)', [
                ('mgmt_cluster', 'Management Cluster', 'Connect to VVF management cluster hosts'),
                ('exit_maintenance', 'Exit Maintenance Mode', 'Remove hosts from maintenance mode'),
                ('datastore', 'Datastore Verification', 'Verify VVF management datastore'),
                ('nsx_mgr', 'NSX Manager', 'Start and verify NSX Manager VM'),
                ('nsx_edges', 'NSX Edge VMs', 'Start NSX Edge virtual machines'),
                ('vcenter', 'vCenter Server', 'Start and verify vCenter Server'),
            ]),
            
            # Group 5: vSphere.py - vSphere Configuration
            ('vsphere', '5. vSphere Configuration (vSphere.py)', [
                ('vcenter_wait', 'Wait for vCenter', 'Wait for vCenter to become available'),
                ('vcenter_connect', 'vCenter Connection', 'Connect to vCenter servers'),
                ('datastores', 'Datastore Verification', 'Verify all datastores are accessible'),
                ('maintenance', 'Exit Maintenance Mode', 'Exit hosts from maintenance mode'),
                ('vcls', 'vCLS Verification', 'Verify vCLS VMs are running'),
                ('drs', 'DRS Configuration', 'Configure DRS settings'),
                ('shell_warning', 'Shell Warning Suppress', 'Suppress ESXi shell warnings'),
                ('vcenter_ready', 'vCenter Ready', 'Verify vCenter UI is accessible'),
                ('power_on_vms', 'Power On VMs', 'Power on configured virtual machines'),
                ('power_on_vapps', 'Power On vApps', 'Power on configured vApps'),
                ('nested_vms', 'Nested VMs Complete', 'All VM startup tasks completed'),
            ]),
            
            # Group 6: pings.py - Network Connectivity
            ('pings', '6. Network Connectivity (pings.py)', [
                ('ping_targets', 'Ping Targets', 'Verify IP connectivity to configured hosts'),
            ]),
            
            # Group 7: services.py - Service Verification
            ('services', '7. Service Verification (services.py)', [
                ('linux_services', 'Linux Services', 'Start and verify Linux services'),
                ('tcp_ports', 'TCP Port Checks', 'Verify service ports are responding'),
            ]),
            
            # Group 8: Kubernetes.py - Kubernetes Certificates
            ('kubernetes', '8. Kubernetes Certificates (Kubernetes.py)', [
                ('cert_check', 'Certificate Check', 'Check Kubernetes certificate expiration'),
                ('cert_renew', 'Certificate Renewal', 'Renew expired certificates if needed'),
            ]),
            
            # Group 9: VCFfinal.py - VCF Final Tasks
            ('vcffinal', '9. VCF Final Tasks (VCFfinal.py)', [
                ('tanzu_control', 'Tanzu Control Plane', 'Start Supervisor control plane VMs'),
                ('tanzu_workload', 'Tanzu Workload Cluster', 'Start workload cluster VMs'),
                ('aria_vms', 'Aria VMs', 'Start Aria Automation virtual machines'),
                ('aria_urls', 'Aria URL Verification', 'Verify Aria Automation URLs'),
            ]),
            
            # Group 10: urls.py - URL Verification
            ('urls', '10. URL Verification (urls.py)', [
                ('url_checks', 'URL Checks', 'Verify all configured web interfaces'),
            ]),
            
            # Group 11: odyssey.py - Odyssey Installation
            ('odyssey', '11. Odyssey Installation (odyssey.py)', [
                ('cleanup', 'Odyssey Cleanup', 'Remove existing Odyssey files'),
                ('install', 'Odyssey Install', 'Download and install Odyssey client'),
                ('shortcut', 'Desktop Shortcut', 'Create desktop shortcut'),
            ]),
            
            # Group 12: final.py - Final Checks
            ('final', '12. Final Checks (final.py)', [
                ('custom', 'Custom Checks', 'Lab-specific final checks'),
                ('labcheck', 'LabCheck Schedule', 'Configure labcheck scheduled task'),
                ('holuser_lock', 'holuser lock', 'Lock holuser account if configured'),
                ('ready', 'Lab Ready', 'Mark lab as ready'),
            ]),
        ]
        
        for group_id, group_name, tasks in default_groups:
            task_list = [
                Task(id=f'{group_id}_{t[0]}', name=t[1], description=t[2])
                for t in tasks
            ]
            self.groups[group_id] = TaskGroup(id=group_id, name=group_name, tasks=task_list)
    
    def update_task(self, group_id: str, task_id: str, status, message: str = ""):
        """
        Update a specific task status
        
        :param group_id: Group identifier
        :param task_id: Task identifier (without group prefix)
        :param status: Status string (pending, running, complete, failed, skipped) or TaskStatus enum
        :param message: Optional status message
        """
        if group_id not in self.groups:
            return
        
        # Handle both string and TaskStatus enum
        if isinstance(status, TaskStatus):
            status_enum = status
        else:
            status_enum = TaskStatus(status.lower())
        
        full_task_id = f'{group_id}_{task_id}'
        
        for task in self.groups[group_id].tasks:
            if task.id == full_task_id:
                if status_enum == TaskStatus.RUNNING and task.start_time is None:
                    task.start_time = datetime.datetime.now()
                elif status_enum in (TaskStatus.COMPLETE, TaskStatus.FAILED, TaskStatus.SKIPPED):
                    task.end_time = datetime.datetime.now()
                
                task.status = status_enum
                task.message = message
                break
        
        self._save_state()
        self.generate_html()
    
    def set_failed(self, reason: str):
        """Mark the entire startup as failed"""
        self.failed = True
        self.failure_reason = reason
        self.generate_html()
    
    def skip_group(self, group_id: str, message: str = "Not applicable"):
        """
        Skip all tasks in a group.
        
        Use this to mark an entire group as skipped when it doesn't apply
        to the current lab type (e.g., skip VVF when running VCF).
        
        :param group_id: Group identifier to skip
        :param message: Optional message explaining why skipped
        """
        if group_id not in self.groups:
            return
        
        for task in self.groups[group_id].tasks:
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.SKIPPED
                task.message = message
        
        self._save_state()
        self.generate_html()
    
    def set_complete(self):
        """Mark the entire startup as complete"""
        for group in self.groups.values():
            for task in group.tasks:
                if task.status == TaskStatus.PENDING:
                    task.status = TaskStatus.SKIPPED
        self._save_state()
        self.generate_html()
    
    def _load_state(self):
        """Load state from JSON file if it exists and matches current lab_sku"""
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                
                # Only load state if it's for the same lab SKU
                if state.get('lab_sku') == self.lab_sku:
                    # Restore start time
                    if 'start_time' in state:
                        self.start_time = datetime.datetime.fromisoformat(state['start_time'])
                    
                    # Restore failure state
                    self.failed = state.get('failed', False)
                    self.failure_reason = state.get('failure_reason', '')
                    
                    # Restore task statuses
                    if 'groups' in state:
                        for gid, group_state in state['groups'].items():
                            if gid in self.groups:
                                for task_state in group_state.get('tasks', []):
                                    task_id = task_state.get('id', '')
                                    for task in self.groups[gid].tasks:
                                        if task.id == task_id:
                                            task.status = TaskStatus(task_state.get('status', 'pending'))
                                            task.message = task_state.get('message', '')
                                            break
        except Exception:
            # If loading fails, continue with fresh state
            pass
    
    def _save_state(self):
        """Save current state to JSON file"""
        state = {
            'lab_sku': self.lab_sku,
            'start_time': self.start_time.isoformat(),
            'failed': self.failed,
            'failure_reason': self.failure_reason,
            'groups': {}
        }
        
        for gid, group in self.groups.items():
            state['groups'][gid] = {
                'name': group.name,
                'tasks': [
                    {
                        'id': t.id,
                        'name': t.name,
                        'status': t.status.value,
                        'message': t.message
                    }
                    for t in group.tasks
                ]
            }
        
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass
    
    def _get_overall_progress(self) -> float:
        """Calculate overall progress percentage"""
        total_tasks = sum(len(g.tasks) for g in self.groups.values())
        if total_tasks == 0:
            return 0.0
        
        completed = sum(
            1 for g in self.groups.values() 
            for t in g.tasks 
            if t.status in [TaskStatus.COMPLETE, TaskStatus.SKIPPED]
        )
        return (completed / total_tasks) * 100
    
    def _get_elapsed_time(self) -> str:
        """Get elapsed time as formatted string"""
        elapsed = datetime.datetime.now() - self.start_time
        minutes = int(elapsed.total_seconds() // 60)
        seconds = int(elapsed.total_seconds() % 60)
        return f"{minutes}m {seconds}s"
    
    def generate_html(self) -> str:
        """Generate the HTML status page"""
        progress = self._get_overall_progress()
        elapsed = self._get_elapsed_time()
        
        # Determine overall status
        if self.failed:
            overall_status = "FAILED"
            status_color = "#ef4444"
        elif progress >= 100:
            overall_status = "READY"
            status_color = "#22c55e"
        else:
            # Check if any task is currently running or has completed
            has_running = any(
                t.status == TaskStatus.RUNNING 
                for g in self.groups.values() 
                for t in g.tasks
            )
            has_completed = any(
                t.status in [TaskStatus.COMPLETE, TaskStatus.FAILED, TaskStatus.SKIPPED]
                for g in self.groups.values() 
                for t in g.tasks
            )
            
            if has_running or has_completed:
                overall_status = "RUNNING"
                status_color = "#f59e0b"  # Amber/orange for running
            else:
                overall_status = "STARTING"
                status_color = "#3b82f6"
        
        html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="{REFRESH_SECONDS}">
    <title>{self.lab_sku} - Lab Startup Status</title>
    <style>
        :root {{
            --bg-primary: #0f172a;
            --bg-secondary: #1e293b;
            --bg-tertiary: #334155;
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --accent-blue: #3b82f6;
            --accent-green: #22c55e;
            --accent-red: #ef4444;
            --accent-yellow: #eab308;
            --accent-purple: #8b5cf6;
        }}
        
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            padding: 2rem;
        }}
        
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        
        header {{
            text-align: center;
            margin-bottom: 2rem;
            padding-bottom: 1.5rem;
            border-bottom: 1px solid var(--bg-tertiary);
        }}
        
        h1 {{
            font-size: 2.5rem;
            font-weight: 700;
            margin-bottom: 0.5rem;
            background: linear-gradient(135deg, var(--accent-blue), var(--accent-purple));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }}
        
        .status-badge {{
            display: inline-block;
            padding: 0.5rem 1.5rem;
            border-radius: 9999px;
            font-weight: 600;
            font-size: 1.1rem;
            background: {status_color};
            color: white;
            margin: 1rem 0;
        }}
        
        .meta-info {{
            display: flex;
            justify-content: center;
            gap: 2rem;
            color: var(--text-secondary);
            font-size: 0.9rem;
        }}
        
        .progress-container {{
            background: var(--bg-secondary);
            border-radius: 1rem;
            padding: 1.5rem;
            margin-bottom: 2rem;
        }}
        
        .progress-bar {{
            height: 1.5rem;
            background: var(--bg-tertiary);
            border-radius: 0.75rem;
            overflow: hidden;
            margin-bottom: 0.5rem;
        }}
        
        .progress-fill {{
            height: 100%;
            background: linear-gradient(90deg, var(--accent-blue), var(--accent-green));
            border-radius: 0.75rem;
            transition: width 0.5s ease;
            width: {progress:.1f}%;
        }}
        
        .progress-text {{
            text-align: center;
            color: var(--text-secondary);
        }}
        
        .task-groups {{
            display: grid;
            gap: 1.5rem;
        }}
        
        .task-group {{
            background: var(--bg-secondary);
            border-radius: 1rem;
            padding: 1.5rem;
            border-left: 4px solid var(--accent-blue);
        }}
        
        .task-group.complete {{
            border-left-color: var(--accent-green);
        }}
        
        .task-group.failed {{
            border-left-color: var(--accent-red);
        }}
        
        .task-group.running {{
            border-left-color: var(--accent-yellow);
        }}
        
        .group-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1rem;
            cursor: pointer;
            user-select: none;
        }}
        
        .group-header:hover {{
            opacity: 0.9;
        }}
        
        .group-title {{
            font-size: 1.25rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        
        .group-toggle {{
            font-size: 0.8rem;
            transition: transform 0.3s ease;
        }}
        
        .group-toggle.collapsed {{
            transform: rotate(-90deg);
        }}
        
        .group-status-icon {{
            font-size: 1.2rem;
        }}
        
        .group-progress {{
            font-size: 0.9rem;
            color: var(--text-secondary);
        }}
        
        .tasks {{
            display: grid;
            gap: 0.75rem;
            transition: max-height 0.3s ease, opacity 0.3s ease;
            overflow: hidden;
        }}
        
        .tasks.collapsed {{
            max-height: 0;
            opacity: 0;
            margin-top: 0;
        }}
        
        .tasks.expanded {{
            max-height: 2000px;
            opacity: 1;
        }}
        
        .task {{
            display: flex;
            align-items: center;
            gap: 1rem;
            padding: 0.75rem 1rem;
            background: var(--bg-tertiary);
            border-radius: 0.5rem;
        }}
        
        .task-icon {{
            font-size: 1.25rem;
            width: 2rem;
            text-align: center;
        }}
        
        .task-info {{
            flex: 1;
        }}
        
        .task-name {{
            font-weight: 500;
        }}
        
        .task-desc {{
            font-size: 0.8rem;
            color: var(--text-secondary);
        }}
        
        .task-message {{
            font-size: 0.8rem;
            color: var(--accent-yellow);
        }}
        
        .legend {{
            margin-top: 2rem;
            padding: 1.5rem;
            background: var(--bg-secondary);
            border-radius: 1rem;
        }}
        
        .legend h3 {{
            margin-bottom: 1rem;
            color: var(--text-secondary);
        }}
        
        .legend-items {{
            display: flex;
            flex-wrap: wrap;
            gap: 1.5rem;
        }}
        
        .legend-item {{
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        
        .failure-banner {{
            background: #fecaca;
            border: 2px solid var(--accent-red);
            color: #7f1d1d;
            padding: 1.5rem;
            border-radius: 1rem;
            margin-bottom: 2rem;
            text-align: center;
        }}
        
        .failure-banner h2 {{
            margin-bottom: 0.5rem;
            color: #991b1b;
        }}
        
        .failure-banner .fail-icon {{
            font-size: 2rem;
            margin-bottom: 0.5rem;
        }}
        
        .auto-refresh {{
            text-align: center;
            color: var(--text-secondary);
            font-size: 0.8rem;
            margin-top: 2rem;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>{self.lab_sku}</h1>
            <div class="status-badge">{overall_status}</div>
            <div class="meta-info">
                <span>Started: {self.start_time.strftime('%H:%M:%S')}</span>
                <span>Elapsed: {elapsed}</span>
                <span>Last Updated: {datetime.datetime.now().strftime('%H:%M:%S')}</span>
            </div>
        </header>
'''
        
        # Add failure banner if failed
        if self.failed:
            html += f'''
        <div class="failure-banner">
            <h2>⚠️ Lab Startup Failed</h2>
            <p>{self.failure_reason}</p>
        </div>
'''
        
        # Progress bar
        html += f'''
        <div class="progress-container">
            <div class="progress-bar">
                <div class="progress-fill"></div>
            </div>
            <div class="progress-text">{progress:.0f}% Complete</div>
        </div>
        
        <div class="task-groups">
'''
        
        # Task groups
        for group in self.groups.values():
            group_class = ""
            group_status_icon = "🔄"
            is_complete = group.status == TaskStatus.COMPLETE
            is_skipped = group.status == TaskStatus.SKIPPED
            
            if group.status == TaskStatus.COMPLETE:
                group_class = "complete"
                group_status_icon = "✅"
            elif group.status == TaskStatus.FAILED:
                group_class = "failed"
                group_status_icon = "❌"
            elif group.status == TaskStatus.RUNNING:
                group_class = "running"
                group_status_icon = "🔄"
            elif group.status == TaskStatus.SKIPPED:
                group_class = "complete"  # Use complete style (collapsed, muted)
                group_status_icon = "⏭️"
            else:
                group_status_icon = "⏳"
            
            # Complete and skipped groups are collapsed by default
            tasks_class = "collapsed" if (is_complete or is_skipped) else "expanded"
            toggle_class = "collapsed" if (is_complete or is_skipped) else ""
            
            html += f'''
            <div class="task-group {group_class}">
                <div class="group-header" onclick="toggleGroup(this)">
                    <span class="group-title">
                        <span class="group-toggle {toggle_class}">▼</span>
                        {group.name}
                        <span class="group-status-icon">{group_status_icon}</span>
                    </span>
                    <span class="group-progress">{group.progress:.0f}%</span>
                </div>
                <div class="tasks {tasks_class}">
'''
            
            for task in group.tasks:
                icon, color, tooltip = self.STATUS_ICONS[task.status]
                html += f'''
                    <div class="task" title="{tooltip}">
                        <span class="task-icon">{icon}</span>
                        <div class="task-info">
                            <div class="task-name">{task.name}</div>
                            <div class="task-desc">{task.description}</div>
'''
                if task.message:
                    html += f'''
                            <div class="task-message">{task.message}</div>
'''
                html += '''
                        </div>
                    </div>
'''
            
            html += '''
                </div>
            </div>
'''
        
        html += '''
        </div>
        
        <div class="legend">
            <h3>Status Legend</h3>
            <div class="legend-items">
'''
        
        for status, (icon, color, tooltip) in self.STATUS_ICONS.items():
            html += f'''
                <div class="legend-item">
                    <span>{icon}</span>
                    <span>{status.value.title()}</span>
                </div>
'''
        
        html += f'''
            </div>
        </div>
        
        <div class="auto-refresh">
            Auto-refreshing every {REFRESH_SECONDS} seconds
        </div>
    </div>
    
    <script>
        function toggleGroup(header) {{
            const tasks = header.nextElementSibling;
            const toggle = header.querySelector('.group-toggle');
            
            if (tasks.classList.contains('collapsed')) {{
                tasks.classList.remove('collapsed');
                tasks.classList.add('expanded');
                toggle.classList.remove('collapsed');
            }} else {{
                tasks.classList.remove('expanded');
                tasks.classList.add('collapsed');
                toggle.classList.add('collapsed');
            }}
        }}
    </script>
</body>
</html>
'''
        
        # Write to file
        try:
            os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
            with open(STATUS_FILE, 'w') as f:
                f.write(html)
        except Exception as e:
            print(f'Error writing status dashboard: {e}')
        
        return html


#==============================================================================
# INITIALIZATION FUNCTIONS
#==============================================================================

def init_dashboard(lab_sku: str = "INITIALIZING") -> 'StatusDashboard':
    """
    Initialize/reset the dashboard to a clean state.
    Clears previous lab run information and creates a fresh dashboard.
    
    :param lab_sku: The lab SKU to display (default: "INITIALIZING")
    """
    # Remove existing state file
    try:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
    except Exception:
        pass
    
    # Create a fresh dashboard
    dashboard = StatusDashboard(lab_sku)
    dashboard.generate_html()
    
    return dashboard


def clear_dashboard() -> None:
    """
    Clear the dashboard completely, removing both the HTML and state files.
    Creates an empty/minimal HTML file to indicate waiting state.
    """
    # Remove existing files
    try:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
    except Exception:
        pass
    
    # Write a minimal "waiting" page
    waiting_html = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="10">
    <title>Lab Startup - Initializing</title>
    <style>
        body {
            font-family: 'Segoe UI', system-ui, sans-serif;
            background: #0f172a;
            color: #f8fafc;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
        }
        .container {
            text-align: center;
            padding: 2rem;
        }
        h1 {
            font-size: 2rem;
            margin-bottom: 1rem;
            color: #3b82f6;
        }
        .spinner {
            width: 50px;
            height: 50px;
            border: 4px solid #334155;
            border-top-color: #3b82f6;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 2rem auto;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        p {
            color: #94a3b8;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Lab Startup Initializing</h1>
        <div class="spinner"></div>
        <p>Waiting for lab startup to begin...</p>
        <p style="font-size: 0.8rem;">Auto-refreshing every 10 seconds</p>
    </div>
</body>
</html>
'''
    
    try:
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        with open(STATUS_FILE, 'w') as f:
            f.write(waiting_html)
    except Exception as e:
        print(f'Error clearing dashboard: {e}')


#==============================================================================
# STANDALONE EXECUTION
#==============================================================================

if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='HOLFY27 Status Dashboard')
    parser.add_argument('--sku', default='HOL-2701', help='Lab SKU')
    parser.add_argument('--demo', action='store_true', help='Generate demo dashboard')
    parser.add_argument('--init', action='store_true', 
                        help='Initialize/reset dashboard to clean state')
    parser.add_argument('--clear', action='store_true',
                        help='Clear dashboard completely (minimal waiting page)')
    
    args = parser.parse_args()
    
    if args.clear:
        # Clear completely - show waiting page
        clear_dashboard()
        print(f'Dashboard cleared at: {STATUS_FILE}')
    elif args.init:
        # Initialize with fresh state
        dashboard = init_dashboard(args.sku)
        print(f'Dashboard initialized for {args.sku} at: {STATUS_FILE}')
    else:
        dashboard = StatusDashboard(args.sku)
        
        if args.demo:
            # Simulate some progress through the startup sequence
            # Group 1: prelim - some complete, one running
            dashboard.update_task('prelim', 'readme', 'complete')
            dashboard.update_task('prelim', 'update_manager', 'complete')
            dashboard.update_task('prelim', 'dns', 'complete')
            dashboard.update_task('prelim', 'dns_import', 'complete')
            dashboard.update_task('prelim', 'firewall', 'complete')
            dashboard.update_task('prelim', 'proxy_filter', 'complete')
            
            # Group 2: esxi - complete
            dashboard.update_task('esxi', 'host_check', 'complete')
            dashboard.update_task('esxi', 'host_ports', 'complete')
            
            # Group 3: vcf - running
            dashboard.update_task('vcf', 'mgmt_cluster', 'complete')
            dashboard.update_task('vcf', 'exit_maintenance', 'complete')
            dashboard.update_task('vcf', 'datastore', 'complete')
            dashboard.update_task('vcf', 'nsx_mgr', 'running', 'Waiting for NSX Manager to start...')
            
            # Group 4: vvf - skipped (VCF lab)
            dashboard.skip_group('vvf', 'VCF lab - VVF not applicable')
        
        dashboard.generate_html()
        print(f'Dashboard generated at: {STATUS_FILE}')

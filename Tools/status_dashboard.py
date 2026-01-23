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
    
    def __init__(self, lab_sku: str):
        self.lab_sku = lab_sku
        self.start_time = datetime.datetime.now()
        self.groups: Dict[str, TaskGroup] = {}
        self.failed = False
        self.failure_reason = ""
        self._init_default_groups()
    
    def _init_default_groups(self):
        """Initialize default task groups based on startup sequence"""
        default_groups = [
            ('prelim', 'Preliminary Checks', [
                ('dns', 'DNS Health Checks', 'Verify DNS resolution for all sites'),
                ('readme', 'README Sync', 'Synchronize README to console'),
                ('firewall', 'Firewall Verification', 'Confirm firewall is active'),
                ('odyssey_cleanup', 'Odyssey Cleanup', 'Remove existing Odyssey files')
            ]),
            ('infrastructure', 'Infrastructure Startup', [
                ('esxi', 'ESXi Hosts', 'Verify nested ESXi hosts are responding'),
                ('vcf', 'VCF Components', 'Start VCF management components'),
                ('nsx', 'NSX Managers', 'Start and verify NSX managers'),
                ('vcenter', 'vCenter Servers', 'Start and connect vCenter servers')
            ]),
            ('vsphere', 'vSphere Configuration', [
                ('datastores', 'Datastore Verification', 'Verify all datastores are accessible'),
                ('maintenance', 'Maintenance Mode', 'Exit hosts from maintenance mode'),
                ('vcls', 'vCLS Verification', 'Verify vCLS VMs are running'),
                ('drs', 'DRS Configuration', 'Configure DRS settings'),
                ('nested_vms', 'Nested VMs', 'Power on nested virtual machines')
            ]),
            ('services', 'Service Verification', [
                ('pings', 'Network Connectivity', 'Verify IP connectivity'),
                ('tcp_ports', 'TCP Port Checks', 'Verify service ports are responding'),
                ('linux_services', 'Linux Services', 'Start and verify Linux services')
            ]),
            ('tanzu', 'Tanzu & Automation', [
                ('supervisor', 'Supervisor VMs', 'Start Supervisor Control Plane VMs'),
                ('tanzu_deploy', 'Tanzu Deployment', 'Deploy Tanzu components'),
                ('aria', 'Aria Automation', 'Start and verify Aria Automation')
            ]),
            ('final', 'Final Checks', [
                ('url_checks', 'URL Verification', 'Verify all web interfaces'),
                ('dns_import', 'DNS Record Import', 'Import custom DNS records'),
                ('custom', 'Custom Checks', 'Lab-specific final checks'),
                ('odyssey', 'Odyssey Installation', 'Install Odyssey client if enabled')
            ])
        ]
        
        for group_id, group_name, tasks in default_groups:
            task_list = [
                Task(id=f'{group_id}_{t[0]}', name=t[1], description=t[2])
                for t in tasks
            ]
            self.groups[group_id] = TaskGroup(id=group_id, name=group_name, tasks=task_list)
    
    def update_task(self, group_id: str, task_id: str, status: str, message: str = ""):
        """
        Update a specific task status
        
        :param group_id: Group identifier
        :param task_id: Task identifier (without group prefix)
        :param status: Status string (pending, running, complete, failed, skipped)
        :param message: Optional status message
        """
        if group_id not in self.groups:
            return
        
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
    
    def set_complete(self):
        """Mark the entire startup as complete"""
        for group in self.groups.values():
            for task in group.tasks:
                if task.status == TaskStatus.PENDING:
                    task.status = TaskStatus.SKIPPED
        self.generate_html()
    
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
            
            if group.status == TaskStatus.COMPLETE:
                group_class = "complete"
                group_status_icon = "✅"
            elif group.status == TaskStatus.FAILED:
                group_class = "failed"
                group_status_icon = "❌"
            elif group.status == TaskStatus.RUNNING:
                group_class = "running"
                group_status_icon = "🔄"
            else:
                group_status_icon = "⏳"
            
            # Complete groups are collapsed by default
            tasks_class = "collapsed" if is_complete else "expanded"
            toggle_class = "collapsed" if is_complete else ""
            
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
            # Simulate some progress
            dashboard.update_task('prelim', 'dns', 'complete')
            dashboard.update_task('prelim', 'readme', 'complete')
            dashboard.update_task('prelim', 'firewall', 'running', 'Checking firewall status...')
            dashboard.update_task('infrastructure', 'esxi', 'running')
        
        dashboard.generate_html()
        print(f'Dashboard generated at: {STATUS_FILE}')

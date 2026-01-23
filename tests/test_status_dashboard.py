#!/usr/bin/env python3
# test_status_dashboard.py - HOLFY27 Status Dashboard Unit Tests
# Version 1.0 - January 2026
# Author - Burke Azbill and HOL Core Team

import pytest
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestTaskStatus:
    """Test TaskStatus enum"""
    
    def test_task_status_values(self):
        """Test TaskStatus enum values"""
        from status_dashboard import TaskStatus
        
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.RUNNING.value == "running"
        assert TaskStatus.COMPLETE.value == "complete"
        assert TaskStatus.FAILED.value == "failed"
        assert TaskStatus.SKIPPED.value == "skipped"


class TestTask:
    """Test Task dataclass"""
    
    def test_task_creation(self):
        """Test creating a Task"""
        from status_dashboard import Task, TaskStatus
        
        task = Task(
            id='test_task',
            name='Test Task',
            description='A test task'
        )
        
        assert task.id == 'test_task'
        assert task.name == 'Test Task'
        assert task.description == 'A test task'
        assert task.status == TaskStatus.PENDING
        assert task.start_time is None
        assert task.end_time is None
        assert task.message == ""
    
    def test_task_with_status(self):
        """Test creating a Task with custom status"""
        from status_dashboard import Task, TaskStatus
        
        task = Task(
            id='test_task',
            name='Test Task',
            description='A test task',
            status=TaskStatus.RUNNING
        )
        
        assert task.status == TaskStatus.RUNNING


class TestTaskGroup:
    """Test TaskGroup dataclass"""
    
    def test_task_group_creation(self):
        """Test creating a TaskGroup"""
        from status_dashboard import TaskGroup, Task
        
        group = TaskGroup(
            id='test_group',
            name='Test Group'
        )
        
        assert group.id == 'test_group'
        assert group.name == 'Test Group'
        assert len(group.tasks) == 0
    
    def test_task_group_status_empty(self):
        """Test TaskGroup status when empty"""
        from status_dashboard import TaskGroup, TaskStatus
        
        group = TaskGroup(id='test', name='Test')
        
        assert group.status == TaskStatus.PENDING
    
    def test_task_group_status_all_complete(self):
        """Test TaskGroup status when all tasks complete"""
        from status_dashboard import TaskGroup, Task, TaskStatus
        
        group = TaskGroup(
            id='test',
            name='Test',
            tasks=[
                Task(id='t1', name='Task 1', description='', status=TaskStatus.COMPLETE),
                Task(id='t2', name='Task 2', description='', status=TaskStatus.COMPLETE)
            ]
        )
        
        assert group.status == TaskStatus.COMPLETE
    
    def test_task_group_status_with_failed(self):
        """Test TaskGroup status when one task failed"""
        from status_dashboard import TaskGroup, Task, TaskStatus
        
        group = TaskGroup(
            id='test',
            name='Test',
            tasks=[
                Task(id='t1', name='Task 1', description='', status=TaskStatus.COMPLETE),
                Task(id='t2', name='Task 2', description='', status=TaskStatus.FAILED)
            ]
        )
        
        assert group.status == TaskStatus.FAILED
    
    def test_task_group_progress(self):
        """Test TaskGroup progress calculation"""
        from status_dashboard import TaskGroup, Task, TaskStatus
        
        group = TaskGroup(
            id='test',
            name='Test',
            tasks=[
                Task(id='t1', name='Task 1', description='', status=TaskStatus.COMPLETE),
                Task(id='t2', name='Task 2', description='', status=TaskStatus.PENDING),
                Task(id='t3', name='Task 3', description='', status=TaskStatus.COMPLETE),
                Task(id='t4', name='Task 4', description='', status=TaskStatus.PENDING)
            ]
        )
        
        assert group.progress == 50.0


class TestStatusDashboard:
    """Test StatusDashboard class"""
    
    def test_dashboard_creation(self):
        """Test creating a StatusDashboard"""
        from status_dashboard import StatusDashboard
        
        dashboard = StatusDashboard('HOL-2705')
        
        assert dashboard.lab_sku == 'HOL-2705'
        assert dashboard.failed is False
        assert len(dashboard.groups) > 0
    
    def test_dashboard_default_groups(self):
        """Test default task groups are created"""
        from status_dashboard import StatusDashboard
        
        dashboard = StatusDashboard('HOL-2705')
        
        expected_groups = ['prelim', 'infrastructure', 'vsphere', 'services', 'tanzu', 'final']
        
        for group_id in expected_groups:
            assert group_id in dashboard.groups
    
    def test_update_task(self):
        """Test updating a task status"""
        from status_dashboard import StatusDashboard, TaskStatus
        
        dashboard = StatusDashboard('HOL-2705')
        
        # Update a task
        dashboard.update_task('prelim', 'dns', 'running', 'Checking DNS...')
        
        # Find the task
        task = None
        for t in dashboard.groups['prelim'].tasks:
            if t.id == 'prelim_dns':
                task = t
                break
        
        assert task is not None
        assert task.status == TaskStatus.RUNNING
        assert task.message == 'Checking DNS...'
    
    def test_set_failed(self):
        """Test marking dashboard as failed"""
        from status_dashboard import StatusDashboard
        
        dashboard = StatusDashboard('HOL-2705')
        dashboard.set_failed('DNS Health Check Failed')
        
        assert dashboard.failed is True
        assert dashboard.failure_reason == 'DNS Health Check Failed'
    
    def test_generate_html(self):
        """Test HTML generation"""
        from status_dashboard import StatusDashboard
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # Patch the STATUS_FILE location
            with patch('status_dashboard.STATUS_FILE', os.path.join(tmpdir, 'status.htm')):
                dashboard = StatusDashboard('HOL-2705')
                html = dashboard.generate_html()
                
                assert 'HOL-2705' in html
                assert 'auto-refresh' in html.lower()
                assert '<html' in html
    
    def test_html_contains_task_groups(self):
        """Test HTML contains task groups"""
        from status_dashboard import StatusDashboard
        
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('status_dashboard.STATUS_FILE', os.path.join(tmpdir, 'status.htm')):
                dashboard = StatusDashboard('HOL-2705')
                html = dashboard.generate_html()
                
                assert 'Preliminary Checks' in html
                assert 'Infrastructure Startup' in html
                assert 'vSphere Configuration' in html
    
    def test_html_refresh_interval(self):
        """Test HTML has correct refresh interval"""
        from status_dashboard import StatusDashboard, REFRESH_SECONDS
        
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('status_dashboard.STATUS_FILE', os.path.join(tmpdir, 'status.htm')):
                dashboard = StatusDashboard('HOL-2705')
                html = dashboard.generate_html()
                
                assert f'content="{REFRESH_SECONDS}"' in html


class TestStatusIcons:
    """Test status icons configuration"""
    
    def test_all_statuses_have_icons(self):
        """Test all TaskStatus values have icons defined"""
        from status_dashboard import StatusDashboard, TaskStatus
        
        for status in TaskStatus:
            assert status in StatusDashboard.STATUS_ICONS
    
    def test_icon_format(self):
        """Test icon tuple format"""
        from status_dashboard import StatusDashboard, TaskStatus
        
        for status, icon_tuple in StatusDashboard.STATUS_ICONS.items():
            assert len(icon_tuple) == 3
            icon, color, tooltip = icon_tuple
            assert isinstance(icon, str)
            assert color.startswith('#')
            assert isinstance(tooltip, str)

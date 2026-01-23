#!/usr/bin/env python3
# conftest.py - HOLFY27 Pytest Configuration and Fixtures
# Version 1.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Shared fixtures for all test modules

import pytest
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch
from configparser import ConfigParser

# Add parent directory and Tools directory to path for imports
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, parent_dir)
sys.path.insert(0, os.path.join(parent_dir, 'Tools'))

#==============================================================================
# FIXTURES - Mock Objects
#==============================================================================

@pytest.fixture
def mock_lsf():
    """Create a mock lsfunctions module with common attributes"""
    mock = MagicMock()
    
    # Set common attributes
    mock.lab_sku = 'HOL-2705'
    mock.labtype = 'HOL'
    mock.holroot = '/home/holuser/hol'
    mock.vpod_repo = '/vpodrepo/2027-labs/2705'
    mock.mcholroot = '/lmchol/hol'
    mock.mcdesktop = '/lmchol/home/holuser/Desktop'
    mock.holorouter_dir = '/tmp/holorouter'
    
    # Config parser with test values
    mock.config = ConfigParser()
    mock.config.add_section('VPOD')
    mock.config.set('VPOD', 'vPod_SKU', 'HOL-2705')
    mock.config.set('VPOD', 'labtype', 'HOL')
    mock.config.set('VPOD', 'maxminutes', '60')
    
    mock.config.add_section('RESOURCES')
    mock.config.set('RESOURCES', 'ESXiHosts', 'esx-01a.site-a.vcf.lab')
    mock.config.set('RESOURCES', 'vCenters', 'vcsa-01a.site-a.vcf.lab')
    
    mock.config.add_section('VCF')
    mock.config.set('VCF', 'vcfvCenter', 'vcsa-01a.site-a.vcf.lab')
    mock.config.set('VCF', 'vcfnsxmgr', 'nsx-01a.site-a.vcf.lab')
    
    # Mock methods
    mock.write_output = MagicMock()
    mock.write_vpodprogress = MagicMock()
    mock.test_ping = MagicMock(return_value=True)
    mock.test_url = MagicMock(return_value=True)
    mock.test_tcp_port = MagicMock(return_value=True)
    mock.run_command = MagicMock(return_value=MagicMock(returncode=0, stdout='', stderr=''))
    mock.ssh = MagicMock(return_value=MagicMock(returncode=0))
    mock.scp = MagicMock(return_value=MagicMock(returncode=0))
    mock.get_password = MagicMock(return_value='MOCK_PW_CHECK_VALUE')
    mock.labfail = MagicMock()
    
    return mock


@pytest.fixture
def mock_config():
    """Create a mock ConfigParser with test values"""
    config = ConfigParser()
    
    config.add_section('VPOD')
    config.set('VPOD', 'vPod_SKU', 'HOL-2705')
    config.set('VPOD', 'labtype', 'HOL')
    config.set('VPOD', 'maxminutes', '60')
    config.set('VPOD', 'new-dns-records', 'true')
    
    config.add_section('RESOURCES')
    config.set('RESOURCES', 'ESXiHosts', 'esx-01a.site-a.vcf.lab')
    config.set('RESOURCES', 'Pings', '10.1.10.130,10.1.10.131')
    config.set('RESOURCES', 'URLs', 'https://vcsa-01a.site-a.vcf.lab/ui')
    
    config.add_section('VCF')
    config.set('VCF', 'vcfvCenter', 'vcsa-01a.site-a.vcf.lab')
    
    config.add_section('CUSTOM')
    config.set('CUSTOM', 'enable_advanced_demo', 'false')
    
    return config


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def temp_config_ini(temp_dir):
    """Create a temporary config.ini file"""
    config_path = os.path.join(temp_dir, 'config.ini')
    
    config = ConfigParser()
    config.add_section('VPOD')
    config.set('VPOD', 'vPod_SKU', 'HOL-2705')
    config.set('VPOD', 'labtype', 'HOL')
    
    with open(config_path, 'w') as f:
        config.write(f)
    
    return config_path


@pytest.fixture
def temp_vpodrepo(temp_dir):
    """Create a temporary vpodrepo structure"""
    vpod_repo = os.path.join(temp_dir, 'vpodrepo', '2027-labs', '2705')
    os.makedirs(vpod_repo, exist_ok=True)
    
    # Create subdirectories
    os.makedirs(os.path.join(vpod_repo, 'Startup'), exist_ok=True)
    os.makedirs(os.path.join(vpod_repo, 'holorouter'), exist_ok=True)
    os.makedirs(os.path.join(vpod_repo, 'scripts'), exist_ok=True)
    
    # Create a sample config.ini
    config = ConfigParser()
    config.add_section('VPOD')
    config.set('VPOD', 'vPod_SKU', 'HOL-2705')
    
    with open(os.path.join(vpod_repo, 'config.ini'), 'w') as f:
        config.write(f)
    
    return vpod_repo


#==============================================================================
# FIXTURES - Mock Network Operations
#==============================================================================

@pytest.fixture
def mock_subprocess():
    """Mock subprocess.run for command execution tests"""
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='Success',
            stderr=''
        )
        yield mock_run


@pytest.fixture
def mock_socket():
    """Mock socket for network tests"""
    with patch('socket.socket') as mock_sock:
        mock_instance = MagicMock()
        mock_instance.connect_ex.return_value = 0
        mock_sock.return_value = mock_instance
        yield mock_sock


@pytest.fixture
def mock_requests():
    """Mock requests for HTTP tests"""
    with patch('requests.Session') as mock_session:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = 'OK'
        
        mock_instance = MagicMock()
        mock_instance.get.return_value = mock_response
        mock_session.return_value = mock_instance
        
        yield mock_session


#==============================================================================
# FIXTURES - File System
#==============================================================================

@pytest.fixture
def dns_records_csv(temp_vpodrepo):
    """Create a sample DNS records CSV file"""
    csv_path = os.path.join(temp_vpodrepo, 'new-dns-records.csv')
    
    content = """hostname,type,value,zone
gitlab,A,10.1.10.50,site-a.vcf.lab
harbor,A,10.1.10.51,site-a.vcf.lab
"""
    
    with open(csv_path, 'w') as f:
        f.write(content)
    
    return csv_path


@pytest.fixture
def mock_holroot(temp_dir):
    """Create a mock holroot directory structure"""
    holroot = os.path.join(temp_dir, 'home', 'holuser', 'hol')
    os.makedirs(holroot, exist_ok=True)
    
    # Create Startup directory with mock modules
    startup_dir = os.path.join(holroot, 'Startup')
    os.makedirs(startup_dir, exist_ok=True)
    
    # Create a minimal module
    prelim_content = '''
def main():
    pass
'''
    with open(os.path.join(startup_dir, 'prelim.py'), 'w') as f:
        f.write(prelim_content)
    
    return holroot


#==============================================================================
# MARKERS
#==============================================================================

def pytest_configure(config):
    """Configure custom markers"""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests"
    )
    config.addinivalue_line(
        "markers", "network: marks tests that require network access"
    )

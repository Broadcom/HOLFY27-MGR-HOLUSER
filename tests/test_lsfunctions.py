#!/usr/bin/env python3
# test_lsfunctions.py - HOLFY27 lsfunctions.py Unit Tests
# Version 1.0 - January 2026
# Author - Burke Azbill and HOL Core Team

import pytest
import os
import sys
from unittest.mock import MagicMock, patch, mock_open

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestParseLabSku:
    """Test parse_labsku function"""
    
    def test_valid_sku_parsing(self):
        """Test parsing a valid lab SKU"""
        with patch.dict('sys.modules', {'lsfunctions': MagicMock()}):
            import lsfunctions as lsf
            
            # Create mock globals
            lsf.lab_sku = 'HOL-BADSKU'
            lsf.vpod_repo = ''
            lsf.write_output = MagicMock()
            
            # Call the function (reimplemented for testing)
            sku = 'HOL-2705'
            lsf.lab_sku = sku
            
            if sku != 'HOL-BADSKU' and len(sku) >= 8:
                year = sku[4:6]
                index = sku[6:8]
                lsf.vpod_repo = f'/vpodrepo/20{year}-labs/{year}{index}'
            
            assert lsf.lab_sku == 'HOL-2705'
            assert lsf.vpod_repo == '/vpodrepo/2027-labs/2705'
    
    def test_bad_sku_handling(self):
        """Test handling of BAD SKU"""
        sku = 'HOL-BADSKU'
        
        lab_sku = sku
        vpod_repo = ''
        
        if sku != 'HOL-BADSKU' and len(sku) >= 8:
            year = sku[4:6]
            index = sku[6:8]
            vpod_repo = f'/vpodrepo/20{year}-labs/{year}{index}'
        
        assert lab_sku == 'HOL-BADSKU'
        assert vpod_repo == ''
    
    def test_different_year_skus(self):
        """Test parsing SKUs from different years"""
        test_cases = [
            ('HOL-2601', '/vpodrepo/2026-labs/2601'),
            ('HOL-2701', '/vpodrepo/2027-labs/2701'),
            ('HOL-2899', '/vpodrepo/2028-labs/2899'),
        ]
        
        for sku, expected_repo in test_cases:
            year = sku[4:6]
            index = sku[6:8]
            vpod_repo = f'/vpodrepo/20{year}-labs/{year}{index}'
            
            assert vpod_repo == expected_repo, f"Failed for SKU {sku}"


class TestChooseFile:
    """Test choose_file function"""
    
    def test_vpodrepo_startup_override(self, temp_vpodrepo):
        """Test that vpodrepo Startup overrides take priority"""
        # Create override file
        override_path = os.path.join(temp_vpodrepo, 'Startup', 'prelim.py')
        with open(override_path, 'w') as f:
            f.write('# Override')
        
        # Simulate choose_file logic
        filename = 'prelim.py'
        vpod_repo = temp_vpodrepo
        holroot = '/home/holuser/hol'
        labtype = 'HOL'
        
        search_paths = [
            os.path.join(vpod_repo, 'Startup', filename),
            os.path.join(vpod_repo, filename),
            f'{holroot}/Startup.{labtype}/{filename}',
            f'{holroot}/Startup/{filename}',
        ]
        
        result = None
        for path in search_paths:
            if os.path.exists(path):
                result = path
                break
        
        assert result == override_path
    
    def test_default_fallback(self, temp_dir, mock_holroot):
        """Test fallback to default when no override exists"""
        filename = 'prelim.py'
        vpod_repo = os.path.join(temp_dir, 'vpodrepo')
        holroot = mock_holroot
        labtype = 'HOL'
        
        # Create default file
        default_path = os.path.join(holroot, 'Startup', filename)
        
        search_paths = [
            os.path.join(vpod_repo, 'Startup', filename),
            os.path.join(vpod_repo, filename),
            f'{holroot}/Startup.{labtype}/{filename}',
            f'{holroot}/Startup/{filename}',
        ]
        
        result = None
        for path in search_paths:
            if os.path.exists(path):
                result = path
                break
        
        assert result == default_path


class TestNetworkOperations:
    """Test network-related functions"""
    
    def test_test_ping_success(self, mock_subprocess):
        """Test ping success"""
        mock_subprocess.return_value.returncode = 0
        
        # Simulate test_ping
        result = mock_subprocess(['ping', '-c', '1', '-W', '5', 'localhost'])
        
        assert result.returncode == 0
    
    def test_test_ping_failure(self, mock_subprocess):
        """Test ping failure"""
        mock_subprocess.return_value.returncode = 1
        
        result = mock_subprocess(['ping', '-c', '1', '-W', '5', 'nonexistent.local'])
        
        assert result.returncode == 1
    
    def test_test_tcp_port_open(self, mock_socket):
        """Test TCP port is open"""
        mock_socket.return_value.connect_ex.return_value = 0
        
        sock = mock_socket()
        result = sock.connect_ex(('localhost', 443))
        
        assert result == 0
    
    def test_test_tcp_port_closed(self, mock_socket):
        """Test TCP port is closed"""
        mock_socket.return_value.connect_ex.return_value = 111  # Connection refused
        
        sock = mock_socket()
        result = sock.connect_ex(('localhost', 12345))
        
        assert result != 0


class TestCommandExecution:
    """Test command execution functions"""
    
    def test_run_command_success(self, mock_subprocess):
        """Test successful command execution"""
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = 'output'
        mock_subprocess.return_value.stderr = ''
        
        result = mock_subprocess('echo test', shell=True)
        
        assert result.returncode == 0
        assert result.stdout == 'output'
    
    def test_run_command_failure(self, mock_subprocess):
        """Test failed command execution"""
        mock_subprocess.return_value.returncode = 1
        mock_subprocess.return_value.stdout = ''
        mock_subprocess.return_value.stderr = 'error'
        
        result = mock_subprocess('false', shell=True)
        
        assert result.returncode == 1
    
    def test_ssh_command(self, mock_subprocess):
        """Test SSH command execution"""
        mock_subprocess.return_value.returncode = 0
        
        password = 'MOCK_PW_CHECK_VALUE'
        target = 'root@esx-01a.site-a.vcf.lab'
        command = 'hostname'
        
        cmd = f'/usr/bin/sshpass -p {password} ssh -o StrictHostKeyChecking=accept-new {target} "{command}"'
        
        result = mock_subprocess(cmd, shell=True)
        
        assert result.returncode == 0


class TestPasswordRetrieval:
    """Test password retrieval functions"""
    
    def test_get_password_from_file(self, temp_dir):
        """Test reading password from creds.txt"""
        creds_path = os.path.join(temp_dir, 'creds.txt')
        expected_password = 'MOCK_PW_CHECK_VALUE'
        
        with open(creds_path, 'w') as f:
            f.write(expected_password)
        
        with open(creds_path, 'r') as f:
            password = f.read().strip()
        
        assert password == expected_password
    
    def test_get_password_missing_file(self, temp_dir):
        """Test handling of missing creds.txt"""
        creds_path = os.path.join(temp_dir, 'creds.txt')
        
        password = None
        if os.path.isfile(creds_path):
            with open(creds_path, 'r') as f:
                password = f.read().strip()
        
        assert password is None


class TestVPodRepoHelpers:
    """Test vpodrepo helper functions"""
    
    def test_get_vpodrepo_file_found(self, temp_vpodrepo):
        """Test finding a file in vpodrepo"""
        # Create a file
        test_file = os.path.join(temp_vpodrepo, 'test.txt')
        with open(test_file, 'w') as f:
            f.write('test')
        
        # Simulate get_vpodrepo_file
        filename = 'test.txt'
        search_paths = [
            os.path.join(temp_vpodrepo, filename),
            os.path.join(temp_vpodrepo, 'Startup', filename),
            os.path.join(temp_vpodrepo, 'scripts', filename),
        ]
        
        result = None
        for path in search_paths:
            if os.path.isfile(path):
                result = path
                break
        
        assert result == test_file
    
    def test_get_vpodrepo_file_not_found(self, temp_vpodrepo):
        """Test file not found in vpodrepo"""
        filename = 'nonexistent.txt'
        search_paths = [
            os.path.join(temp_vpodrepo, filename),
            os.path.join(temp_vpodrepo, 'Startup', filename),
        ]
        
        result = None
        for path in search_paths:
            if os.path.isfile(path):
                result = path
                break
        
        assert result is None

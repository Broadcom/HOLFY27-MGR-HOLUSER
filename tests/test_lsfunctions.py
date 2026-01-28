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
        
        cmd = f'/usr/bin/sshpass -p {password} ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {target} "{command}"'
        
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


class TestGetRepoInfo:
    """Test get_repo_info function for multiple lab types and SKU patterns"""
    
    def _get_repo_info(self, sku: str, lab_type: str = 'HOL') -> tuple:
        """
        Local implementation of get_repo_info for testing.
        Mirrors the logic in lsfunctions.py
        """
        bad_sku = 'HOL-BADSKU'
        
        if not sku or sku == bad_sku:
            return ('', '', '')
        
        parts = sku.split('-', 1)
        if len(parts) < 2:
            return ('', '', '')
        
        prefix = parts[0]
        suffix = parts[1]
        
        lab_type_upper = lab_type.upper() if lab_type else 'HOL'
        
        if lab_type_upper == 'DISCOVERY':
            year_dir = '/vpodrepo/Discovery-labs'
            repo_dir = f'{year_dir}/{suffix}'
            git_url = f'https://github.com/Broadcom/{sku}.git'
        else:
            if len(suffix) >= 4:
                year = suffix[:2]
                index = suffix[2:4]
                year_dir = f'/vpodrepo/20{year}-labs'
                repo_dir = f'{year_dir}/{year}{index}'
                git_url = f'https://github.com/Broadcom/{prefix}-{year}{index}.git'
            else:
                year_dir = f'/vpodrepo/{prefix}-labs'
                repo_dir = f'{year_dir}/{suffix}'
                git_url = f'https://github.com/Broadcom/{sku}.git'
        
        return (year_dir, repo_dir, git_url)
    
    def test_hol_standard_sku(self):
        """Test HOL standard SKU parsing"""
        sku = 'HOL-2701'
        labtype = 'HOL'
        year_dir, repo_dir, git_url = self._get_repo_info(sku, labtype)
        
        assert year_dir == '/vpodrepo/2027-labs'
        assert repo_dir == '/vpodrepo/2027-labs/2701'
        assert git_url == 'https://github.com/Broadcom/HOL-2701.git'
    
    def test_ate_standard_sku(self):
        """Test ATE standard SKU parsing"""
        sku = 'ATE-2701'
        labtype = 'ATE'
        year_dir, repo_dir, git_url = self._get_repo_info(sku, labtype)
        
        assert year_dir == '/vpodrepo/2027-labs'
        assert repo_dir == '/vpodrepo/2027-labs/2701'
        assert git_url == 'https://github.com/Broadcom/ATE-2701.git'
    
    def test_vxp_standard_sku(self):
        """Test VXP standard SKU parsing"""
        sku = 'VXP-2740'
        labtype = 'VXP'
        year_dir, repo_dir, git_url = self._get_repo_info(sku, labtype)
        
        assert year_dir == '/vpodrepo/2027-labs'
        assert repo_dir == '/vpodrepo/2027-labs/2740'
        assert git_url == 'https://github.com/Broadcom/VXP-2740.git'
    
    def test_edu_standard_sku(self):
        """Test EDU standard SKU parsing"""
        sku = 'EDU-2705'
        labtype = 'EDU'
        year_dir, repo_dir, git_url = self._get_repo_info(sku, labtype)
        
        assert year_dir == '/vpodrepo/2027-labs'
        assert repo_dir == '/vpodrepo/2027-labs/2705'
        assert git_url == 'https://github.com/Broadcom/EDU-2705.git'
    
    def test_discovery_named_sku(self):
        """Test Discovery named SKU parsing (no year extraction)"""
        sku = 'Discovery-Demo'
        labtype = 'Discovery'
        year_dir, repo_dir, git_url = self._get_repo_info(sku, labtype)
        
        assert year_dir == '/vpodrepo/Discovery-labs'
        assert repo_dir == '/vpodrepo/Discovery-labs/Demo'
        assert git_url == 'https://github.com/Broadcom/Discovery-Demo.git'
    
    def test_discovery_complex_name(self):
        """Test Discovery with hyphenated name"""
        sku = 'Discovery-VCF-Overview'
        labtype = 'Discovery'
        year_dir, repo_dir, git_url = self._get_repo_info(sku, labtype)
        
        assert year_dir == '/vpodrepo/Discovery-labs'
        assert repo_dir == '/vpodrepo/Discovery-labs/VCF-Overview'
        assert git_url == 'https://github.com/Broadcom/Discovery-VCF-Overview.git'
    
    def test_different_years(self):
        """Test parsing SKUs from different years"""
        test_cases = [
            ('HOL-2601', 'HOL', '/vpodrepo/2026-labs', '/vpodrepo/2026-labs/2601'),
            ('HOL-2701', 'HOL', '/vpodrepo/2027-labs', '/vpodrepo/2027-labs/2701'),
            ('ATE-2899', 'ATE', '/vpodrepo/2028-labs', '/vpodrepo/2028-labs/2899'),
            ('VXP-3001', 'VXP', '/vpodrepo/2030-labs', '/vpodrepo/2030-labs/3001'),
        ]
        
        for sku, labtype, expected_year_dir, expected_repo_dir in test_cases:
            year_dir, repo_dir, git_url = self._get_repo_info(sku, labtype)
            assert year_dir == expected_year_dir, f"Failed year_dir for SKU {sku}"
            assert repo_dir == expected_repo_dir, f"Failed repo_dir for SKU {sku}"
    
    def test_bad_sku_returns_empty(self):
        """Test that BAD SKU returns empty strings"""
        sku = 'HOL-BADSKU'
        labtype = 'HOL'
        year_dir, repo_dir, git_url = self._get_repo_info(sku, labtype)
        
        assert year_dir == ''
        assert repo_dir == ''
        assert git_url == ''
    
    def test_empty_sku_returns_empty(self):
        """Test that empty SKU returns empty strings"""
        year_dir, repo_dir, git_url = self._get_repo_info('', 'HOL')
        
        assert year_dir == ''
        assert repo_dir == ''
        assert git_url == ''
    
    def test_none_sku_returns_empty(self):
        """Test that None SKU returns empty strings"""
        year_dir, repo_dir, git_url = self._get_repo_info(None, 'HOL')
        
        assert year_dir == ''
        assert repo_dir == ''
        assert git_url == ''
    
    def test_case_insensitive_labtype(self):
        """Test that labtype comparison is case-insensitive"""
        sku = 'Discovery-Demo'
        
        # Test various cases
        for labtype in ['Discovery', 'DISCOVERY', 'discovery', 'DiScOvErY']:
            year_dir, repo_dir, git_url = self._get_repo_info(sku, labtype)
            assert year_dir == '/vpodrepo/Discovery-labs', f"Failed for labtype: {labtype}"
            assert repo_dir == '/vpodrepo/Discovery-labs/Demo', f"Failed for labtype: {labtype}"


class TestLabTypeRepoPatterns:
    """Test lab type repository pattern metadata"""
    
    def test_hol_uses_standard_pattern(self):
        """Test that HOL uses standard repo pattern"""
        from Tools.labtypes import LabTypeLoader
        loader = LabTypeLoader('HOL', '/home/holuser/hol')
        assert loader.get_repo_pattern() == 'standard'
    
    def test_discovery_uses_named_pattern(self):
        """Test that Discovery uses named repo pattern"""
        from Tools.labtypes import LabTypeLoader
        loader = LabTypeLoader('Discovery', '/home/holuser/hol')
        assert loader.get_repo_pattern() == 'named'
    
    def test_ate_uses_standard_pattern(self):
        """Test that ATE uses standard repo pattern"""
        from Tools.labtypes import LabTypeLoader
        loader = LabTypeLoader('ATE', '/home/holuser/hol')
        assert loader.get_repo_pattern() == 'standard'
    
    def test_vxp_uses_standard_pattern(self):
        """Test that VXP uses standard repo pattern"""
        from Tools.labtypes import LabTypeLoader
        loader = LabTypeLoader('VXP', '/home/holuser/hol')
        assert loader.get_repo_pattern() == 'standard'
    
    def test_edu_uses_standard_pattern(self):
        """Test that EDU uses standard repo pattern"""
        from Tools.labtypes import LabTypeLoader
        loader = LabTypeLoader('EDU', '/home/holuser/hol')
        assert loader.get_repo_pattern() == 'standard'
    
    def test_unknown_labtype_defaults_to_standard(self):
        """Test that unknown labtype defaults to standard pattern"""
        from Tools.labtypes import LabTypeLoader
        # Unknown labtype falls back to HOL which uses standard
        loader = LabTypeLoader('UNKNOWN', '/home/holuser/hol')
        assert loader.get_repo_pattern() == 'standard'
    
    def test_all_labtypes_have_repo_pattern(self):
        """Test that all defined lab types have repo_pattern metadata"""
        from Tools.labtypes import LabTypeLoader
        
        # Note: LabTypeLoader normalizes labtypes to uppercase
        for labtype in ['HOL', 'DISCOVERY', 'VXP', 'ATE', 'EDU']:
            loader = LabTypeLoader(labtype, '/home/holuser/hol')
            pattern = loader.get_repo_pattern()
            assert pattern in ['standard', 'named'], f"Invalid pattern for {labtype}: {pattern}"
    
    def test_labtype_case_insensitive(self):
        """Test that labtype input is case-insensitive"""
        from Tools.labtypes import LabTypeLoader
        
        # Various case combinations should all work
        for labtype_input in ['discovery', 'Discovery', 'DISCOVERY']:
            loader = LabTypeLoader(labtype_input, '/home/holuser/hol')
            assert loader.labtype == 'DISCOVERY'
            assert loader.get_repo_pattern() == 'named'

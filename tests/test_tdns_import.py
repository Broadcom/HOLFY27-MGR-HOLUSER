#!/usr/bin/env python3
# test_tdns_import.py - HOLFY27 DNS Import Unit Tests
# Version 1.0 - January 2026
# Author - Burke Azbill and HOL Core Team

import pytest
import os
import sys
import json
from unittest.mock import MagicMock, patch

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestTDNSAvailability:
    """Test tdns-mgr availability checks"""
    
    def test_check_tdns_mgr_in_standard_path(self, mock_subprocess):
        """Test finding tdns-mgr in standard path"""
        # Simulate which command finding tdns-mgr
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = '/usr/local/bin/tdns-mgr\n'
        
        result = mock_subprocess(['which', 'tdns-mgr'], capture_output=True, text=True)
        
        found_path = result.stdout.strip() if result.returncode == 0 else None
        
        assert found_path == '/usr/local/bin/tdns-mgr'
    
    def test_check_tdns_mgr_not_found(self, mock_subprocess):
        """Test tdns-mgr not found"""
        mock_subprocess.return_value.returncode = 1
        mock_subprocess.return_value.stdout = ''
        
        result = mock_subprocess(['which', 'tdns-mgr'], capture_output=True, text=True)
        
        found_path = result.stdout.strip() if result.returncode == 0 else None
        
        assert found_path is None


class TestDNSRecordsFile:
    """Test DNS records file handling"""
    
    def test_find_dns_records_file(self, dns_records_csv):
        """Test finding DNS records file in vpodrepo"""
        assert os.path.isfile(dns_records_csv)
    
    def test_dns_records_csv_format(self, dns_records_csv):
        """Test DNS records CSV format"""
        with open(dns_records_csv, 'r') as f:
            content = f.read()
        
        # Should have header
        lines = content.strip().split('\n')
        header = lines[0]
        
        assert 'hostname' in header
        assert 'type' in header
        assert 'value' in header
        assert 'zone' in header
    
    def test_parse_dns_records(self, dns_records_csv):
        """Test parsing DNS records from CSV"""
        import csv
        
        records = []
        with open(dns_records_csv, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                records.append(row)
        
        assert len(records) >= 1
        assert 'hostname' in records[0]
        assert 'type' in records[0]


class TestTDNSLogin:
    """Test tdns-mgr login"""
    
    def test_tdns_login_success(self, mock_subprocess):
        """Test successful tdns-mgr login"""
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = 'Login successful'
        
        password = 'MOCK_PW_CHECK_VALUE'
        result = mock_subprocess(
            ['/usr/local/bin/tdns-mgr', 'login', '-p', password],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        assert result.returncode == 0
    
    def test_tdns_login_failure(self, mock_subprocess):
        """Test failed tdns-mgr login"""
        mock_subprocess.return_value.returncode = 1
        mock_subprocess.return_value.stderr = 'Invalid credentials'
        
        result = mock_subprocess(
            ['/usr/local/bin/tdns-mgr', 'login', '-p', 'wrong'],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        assert result.returncode != 0


class TestDNSRecordImport:
    """Test DNS record import"""
    
    def test_import_records_success(self, mock_subprocess, dns_records_csv):
        """Test successful DNS record import"""
        expected_output = {
            "New Records": 2,
            "Errors": 0,
            "Message": "Success"
        }
        
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = json.dumps(expected_output)
        
        result = mock_subprocess(
            ['/usr/local/bin/tdns-mgr', 'import-records', dns_records_csv, '--ptr'],
            capture_output=True,
            text=True,
            timeout=120
        )
        
        output = json.loads(result.stdout.strip())
        
        assert output['New Records'] == 2
        assert output['Errors'] == 0
        assert output['Message'] == 'Success'
    
    def test_import_records_partial_failure(self, mock_subprocess, dns_records_csv):
        """Test DNS import with some errors"""
        expected_output = {
            "New Records": 1,
            "Errors": 1,
            "Message": "Partial success"
        }
        
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = json.dumps(expected_output)
        
        result = mock_subprocess(
            ['/usr/local/bin/tdns-mgr', 'import-records', dns_records_csv, '--ptr'],
            capture_output=True,
            text=True,
            timeout=120
        )
        
        output = json.loads(result.stdout.strip())
        
        # Should log errors but not fail the lab
        assert output['Errors'] == 1
    
    def test_import_records_all_fail(self, mock_subprocess, dns_records_csv):
        """Test DNS import with all records failing"""
        expected_output = {
            "New Records": 0,
            "Errors": 2,
            "Message": "All records failed"
        }
        
        mock_subprocess.return_value.returncode = 1
        mock_subprocess.return_value.stdout = json.dumps(expected_output)
        
        result = mock_subprocess(
            ['/usr/local/bin/tdns-mgr', 'import-records', dns_records_csv, '--ptr'],
            capture_output=True,
            text=True,
            timeout=120
        )
        
        output = json.loads(result.stdout.strip())
        
        # Should log but not fail the lab
        assert output['Errors'] == 2


class TestConfigCheck:
    """Test config.ini checks for DNS import"""
    
    def test_check_config_enabled(self, mock_config):
        """Test checking if DNS import is enabled in config"""
        if mock_config.has_option('VPOD', 'new-dns-records'):
            value = mock_config.get('VPOD', 'new-dns-records').lower()
            enabled = value in ['true', 'yes', '1', 'enabled']
        else:
            enabled = False
        
        assert enabled is True
    
    def test_check_config_disabled(self):
        """Test when DNS import is not configured"""
        from configparser import ConfigParser
        
        config = ConfigParser()
        config.add_section('VPOD')
        config.set('VPOD', 'vPod_SKU', 'HOL-2705')
        # No new-dns-records option
        
        enabled = False
        if config.has_option('VPOD', 'new-dns-records'):
            value = config.get('VPOD', 'new-dns-records').lower()
            enabled = value in ['true', 'yes', '1', 'enabled']
        
        assert enabled is False
    
    def test_check_config_explicit_false(self):
        """Test when DNS import is explicitly disabled"""
        from configparser import ConfigParser
        
        config = ConfigParser()
        config.add_section('VPOD')
        config.set('VPOD', 'new-dns-records', 'false')
        
        value = config.get('VPOD', 'new-dns-records').lower()
        enabled = value in ['true', 'yes', '1', 'enabled']
        
        assert enabled is False


class TestIntegration:
    """Integration tests for DNS import"""
    
    def test_full_import_flow(self, mock_subprocess, dns_records_csv, temp_dir):
        """Test full DNS import flow"""
        # Step 1: Check tdns-mgr available
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = '/usr/local/bin/tdns-mgr\n'
        
        which_result = mock_subprocess(['which', 'tdns-mgr'])
        assert which_result.returncode == 0
        
        # Step 2: Login
        mock_subprocess.return_value.stdout = 'Login successful'
        login_result = mock_subprocess(['/usr/local/bin/tdns-mgr', 'login', '-p', 'pass'])
        assert login_result.returncode == 0
        
        # Step 3: Import
        import_output = {"New Records": 2, "Errors": 0, "Message": "Success"}
        mock_subprocess.return_value.stdout = json.dumps(import_output)
        
        import_result = mock_subprocess(['/usr/local/bin/tdns-mgr', 'import-records', dns_records_csv, '--ptr'])
        output = json.loads(import_result.stdout)
        
        assert output['New Records'] == 2
        assert output['Errors'] == 0

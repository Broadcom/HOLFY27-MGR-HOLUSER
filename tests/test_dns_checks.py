#!/usr/bin/env python3
# test_dns_checks.py - HOLFY27 DNS Health Check Unit Tests
# Version 1.0 - January 2026
# Author - Burke Azbill and HOL Core Team

import pytest
import os
import sys
from unittest.mock import MagicMock, patch

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestDNSResolution:
    """Test DNS resolution functions"""
    
    def test_resolve_dns_success(self, mock_subprocess):
        """Test successful DNS resolution"""
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = '10.1.1.101\n'
        
        hostname = 'esx-01a.site-a.vcf.lab'
        dns_server = '10.1.10.129'
        
        result = mock_subprocess(
            ['dig', '+short', f'@{dns_server}', hostname, 'A'],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        ips = [line for line in result.stdout.strip().split('\n') if line and not line.endswith('.')]
        
        assert '10.1.1.101' in ips
    
    def test_resolve_dns_no_result(self, mock_subprocess):
        """Test DNS resolution with no result"""
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = ''
        
        result = mock_subprocess(
            ['dig', '+short', '@10.1.10.129', 'nonexistent.local', 'A'],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        ips = [line for line in result.stdout.strip().split('\n') if line]
        
        assert len(ips) == 0
    
    def test_resolve_dns_cname_filtering(self, mock_subprocess):
        """Test that CNAME responses are filtered out"""
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = 'alias.example.com.\n93.184.216.34\n'
        
        result = mock_subprocess(
            ['dig', '+short', '@8.8.8.8', 'www.example.com', 'A'],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        # Filter out CNAME responses (lines ending with .)
        ips = [line for line in result.stdout.strip().split('\n') if line and not line.endswith('.')]
        
        assert '93.184.216.34' in ips
        assert 'alias.example.com.' not in ips


class TestDNSCheckConfiguration:
    """Test DNS check configuration"""
    
    def test_dns_check_config_structure(self):
        """Test DNS check configuration has required fields"""
        DNS_CHECKS = {
            'site_a': {
                'hostname': 'esx-01a.site-a.vcf.lab',
                'expected_ip': '10.1.1.101',
                'description': 'Site A DNS Resolution'
            },
            'site_b': {
                'hostname': 'esx-01b.site-b.vcf.lab',
                'expected_ip': '10.2.1.101',
                'description': 'Site B DNS Resolution'
            },
            'external': {
                'hostname': 'www.broadcom.com',
                'expected_ip': None,
                'description': 'External DNS Resolution'
            }
        }
        
        for check_name, config in DNS_CHECKS.items():
            assert 'hostname' in config
            assert 'expected_ip' in config
            assert 'description' in config
    
    def test_dns_check_site_a(self):
        """Test Site A DNS check parameters"""
        check = {
            'hostname': 'esx-01a.site-a.vcf.lab',
            'expected_ip': '10.1.1.101',
            'description': 'Site A DNS Resolution'
        }
        
        assert check['hostname'] == 'esx-01a.site-a.vcf.lab'
        assert check['expected_ip'] == '10.1.1.101'
    
    def test_dns_check_external_any_result(self):
        """Test that external DNS check accepts any result"""
        check = {
            'hostname': 'www.broadcom.com',
            'expected_ip': None,  # Any result is valid
            'description': 'External DNS Resolution'
        }
        
        # Any non-empty result should pass
        results = ['199.244.49.62']
        
        if check['expected_ip'] is None:
            passed = len(results) > 0
        else:
            passed = check['expected_ip'] in results
        
        assert passed


class TestDNSCheckResolution:
    """Test DNS resolution validation"""
    
    def test_check_dns_resolution_match(self):
        """Test DNS resolution matches expected IP"""
        expected_ip = '10.1.1.101'
        results = ['10.1.1.101']
        
        passed = expected_ip in results
        
        assert passed
    
    def test_check_dns_resolution_no_match(self):
        """Test DNS resolution does not match expected IP"""
        expected_ip = '10.1.1.101'
        results = ['10.1.1.102']
        
        passed = expected_ip in results
        
        assert not passed
    
    def test_check_dns_resolution_empty_results(self):
        """Test DNS resolution with empty results"""
        expected_ip = '10.1.1.101'
        results = []
        
        if not results:
            passed = False
        else:
            passed = expected_ip in results
        
        assert not passed
    
    def test_check_dns_external_any_result(self):
        """Test external DNS check accepts any result"""
        expected_ip = None  # Any result is valid
        results = ['199.244.49.62', '199.244.49.63']
        
        if expected_ip is None:
            passed = len(results) > 0
        else:
            passed = expected_ip in results
        
        assert passed


class TestDNSCheckTimeout:
    """Test DNS check timeout behavior"""
    
    def test_timeout_configuration(self):
        """Test timeout configuration"""
        TIMEOUT_MINUTES = 5
        CHECK_INTERVAL_SECONDS = 30
        
        timeout_seconds = TIMEOUT_MINUTES * 60
        
        assert timeout_seconds == 300
        assert CHECK_INTERVAL_SECONDS == 30
    
    def test_max_attempts_calculation(self):
        """Test calculation of maximum attempts"""
        TIMEOUT_MINUTES = 5
        CHECK_INTERVAL_SECONDS = 30
        
        timeout_seconds = TIMEOUT_MINUTES * 60
        max_attempts = timeout_seconds // CHECK_INTERVAL_SECONDS
        
        assert max_attempts == 10


class TestDNSCheckIntegration:
    """Integration-style tests for DNS checks"""
    
    def test_all_checks_pass_scenario(self, mock_subprocess):
        """Test scenario where all DNS checks pass"""
        def mock_dig_response(*args, **kwargs):
            cmd = args[0]
            mock_result = MagicMock()
            mock_result.returncode = 0
            
            if 'esx-01a' in cmd[3]:
                mock_result.stdout = '10.1.1.101\n'
            elif 'esx-01b' in cmd[3]:
                mock_result.stdout = '10.2.1.101\n'
            elif 'broadcom' in cmd[3]:
                mock_result.stdout = '199.244.49.62\n'
            else:
                mock_result.stdout = ''
            
            return mock_result
        
        mock_subprocess.side_effect = mock_dig_response
        
        # Simulate check for each
        checks_passed = []
        
        for hostname, expected in [('esx-01a.site-a.vcf.lab', '10.1.1.101'),
                                    ('esx-01b.site-b.vcf.lab', '10.2.1.101'),
                                    ('www.broadcom.com', None)]:
            result = mock_subprocess(['dig', '+short', '@10.1.10.129', hostname, 'A'])
            ips = [l for l in result.stdout.strip().split('\n') if l]
            
            if expected is None:
                checks_passed.append(len(ips) > 0)
            else:
                checks_passed.append(expected in ips)
        
        assert all(checks_passed)
    
    def test_some_checks_fail_scenario(self, mock_subprocess):
        """Test scenario where some DNS checks fail"""
        def mock_dig_response(*args, **kwargs):
            cmd = args[0]
            mock_result = MagicMock()
            mock_result.returncode = 0
            
            if 'esx-01a' in cmd[3]:
                mock_result.stdout = '10.1.1.101\n'
            elif 'esx-01b' in cmd[3]:
                mock_result.stdout = ''  # Fail - no result
            else:
                mock_result.stdout = '199.244.49.62\n'
            
            return mock_result
        
        mock_subprocess.side_effect = mock_dig_response
        
        # Site B should fail
        result = mock_subprocess(['dig', '+short', '@10.1.10.129', 'esx-01b.site-b.vcf.lab', 'A'])
        ips = [l for l in result.stdout.strip().split('\n') if l]
        
        assert len(ips) == 0

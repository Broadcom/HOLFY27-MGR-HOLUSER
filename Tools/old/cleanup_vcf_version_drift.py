#!/usr/bin/env python3
"""
cleanup_vcf_version_drift.py

This script clears stale target upgrade versions in SDDC Manager to resolve 
the "Version Drift" status in VCF Operations after a successful upgrade.

Usage:
    python3 cleanup_vcf_version_drift.py [--sddc-host FQDN] [--user USERNAME] [--password PASSWORD]
"""

import argparse
import getpass
import requests
import urllib3
import sys
import os

# Suppress insecure request warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def get_token(sddc_host, username, password):
    url = f"https://{sddc_host}/v1/tokens"
    payload = {"username": username, "password": password}
    try:
        response = requests.post(url, json=payload, verify=False, timeout=10)
        response.raise_for_status()
        return response.json().get("accessToken")
    except requests.exceptions.RequestException as e:
        print(f"[-] Failed to authenticate to {sddc_host}: {e}")
        sys.exit(1)

def get_domains(sddc_host, headers):
    url = f"https://{sddc_host}/v1/releases/domains"
    try:
        response = requests.get(url, headers=headers, verify=False, timeout=10)
        response.raise_for_status()
        return response.json().get("elements", [])
    except requests.exceptions.RequestException as e:
        print(f"[-] Failed to retrieve domains: {e}")
        sys.exit(1)

def clear_domain_drift(sddc_host, domain_id, headers):
    url = f"https://{sddc_host}/v1/releases/domains/{domain_id}"
    try:
        response = requests.delete(url, headers=headers, verify=False, timeout=10)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        print(f"[-] Failed to clear drift for domain {domain_id}: {e}")
        if 'response' in locals() and response is not None:
            print(f"    Response: {response.text}")
        return False

def main():
    parser = argparse.ArgumentParser(description="Clear VCF Version Drift in SDDC Manager")
    parser.add_argument("--sddc-host", default="sddcmanager-a.site-a.vcf.lab", help="SDDC Manager FQDN or IP")
    parser.add_argument("--user", default="administrator@vsphere.local", help="SSO Username")
    parser.add_argument("--password", help="SSO Password (will prompt if not provided)")
    
    args = parser.parse_args()
    
    password = args.password
    if not password:
        # Fallback to lab default if it exists, otherwise prompt securely
        creds_file = '/home/holuser/creds.txt'
        if os.path.exists(creds_file):
            with open(creds_file, 'r') as f:
                password = f.read().strip()
        else:
            password = getpass.getpass(prompt=f"Enter password for {args.user}: ")

    print(f"[*] Authenticating to SDDC Manager: {args.sddc_host}...")
    token = get_token(args.sddc_host, args.user, password)
    
    # Note: Content-Type is required by this specific API endpoint for the DELETE method
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    print("[*] Fetching domains...")
    domains = get_domains(args.sddc_host, headers)
    
    if not domains:
        print("[-] No domains found.")
        sys.exit(0)

    success_count = 0
    for domain in domains:
        domain_id = domain.get("domainId")
        
        print(f"[*] Checking Domain ID: {domain_id}")
        print(f"    -> Clearing stale target version metadata...")
        
        if clear_domain_drift(args.sddc_host, domain_id, headers):
            print(f"    -> Successfully cleared.")
            success_count += 1

    print(f"\n[*] Cleanup complete. Successfully processed {success_count}/{len(domains)} domains.")
    print("[*] Please refresh the VCF Operations UI to verify the Version Drift is resolved.")

if __name__ == "__main__":
    main()

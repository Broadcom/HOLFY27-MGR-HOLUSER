#!/usr/bin/env python3
# fleet.py - HOLFY27 Fleet Management (SDDC Manager) Operations
# Version 1.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# Provides Fleet Operations API integration for shutdown orchestration

"""
Fleet Management (SDDC Manager) API Integration Module

This module provides functions for interacting with VMware Fleet Operations
(via SDDC Manager/VCF Operations Manager) to orchestrate graceful
shutdown of VCF environments.

Products that can be managed via Fleet Operations:
- vra (VCF Automation)
- vrni (VCF Operations for Networks)  
- vrops (VCF Operations)
- vrli (VCF Operations for Logs)
- vrlcm (VCF Operations Manager itself)

Power operations:
- power-on: Start the product VMs
- power-off: Gracefully shutdown product VMs
"""

import os
import sys
import json
import base64
import time
import logging
import requests
import urllib3

# Disable SSL warnings for lab environment
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Add hol directory to path for lsfunctions access
sys.path.insert(0, '/home/holuser/hol')

# Default logging level
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

#==============================================================================
# MODULE CONFIGURATION
#==============================================================================

DEBUG = False
SSL_VERIFY = False

# Timeouts and retries
REQUEST_TIMEOUT = 30  # seconds for API requests
POWER_OP_POLL_INTERVAL = 45  # seconds between status checks
POWER_OP_MAX_WAIT = 1800  # 30 minutes max wait for power operation
INVENTORY_SYNC_POLL_INTERVAL = 15  # seconds between inventory sync checks
INVENTORY_SYNC_MAX_WAIT = 300  # 5 minutes max wait for inventory sync

#==============================================================================
# TOKEN MANAGEMENT
#==============================================================================

def get_encoded_token(username: str, password: str) -> str:
    """
    Create a base64 encoded token for Fleet Management API authentication.
    
    :param username: Fleet Management username (e.g., admin@local)
    :param password: Fleet Management password
    :return: Base64 encoded credentials string
    """
    credentials = f"{username}:{password}"
    bytes_credentials = credentials.encode('utf-8')
    base64_bytes = base64.b64encode(bytes_credentials)
    return base64_bytes.decode('utf-8')

#==============================================================================
# API HELPERS
#==============================================================================

def _make_request(method: str, url: str, token: str, payload: dict = None, 
                  verify: bool = SSL_VERIFY) -> dict:
    """
    Make an authenticated API request to Fleet Management.
    
    :param method: HTTP method (GET, POST, DELETE, etc.)
    :param url: Full URL endpoint
    :param token: Base64 encoded auth token
    :param payload: Optional request body (dict)
    :param verify: SSL verification flag
    :return: JSON response as dict
    :raises: requests.HTTPError on failure
    """
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Basic {token}',
        'Accept': 'application/json'
    }
    
    try:
        if method.upper() == 'GET':
            response = requests.get(url, headers=headers, verify=verify, 
                                   timeout=REQUEST_TIMEOUT)
        elif method.upper() == 'POST':
            data = json.dumps(payload) if payload else None
            response = requests.post(url, headers=headers, data=data, 
                                    verify=verify, timeout=REQUEST_TIMEOUT)
        elif method.upper() == 'DELETE':
            data = json.dumps(payload) if payload else None
            response = requests.delete(url, headers=headers, data=data,
                                       verify=verify, timeout=REQUEST_TIMEOUT)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        
        response.raise_for_status()
        return response.json() if response.text else {}
        
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP Error: {e}")
        logger.debug(f"Response: {e.response.text if hasattr(e, 'response') else 'N/A'}")
        raise
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection Error: {e}")
        raise
    except requests.exceptions.Timeout as e:
        logger.error(f"Timeout Error: {e}")
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"Request Error: {e}")
        raise

#==============================================================================
# ENVIRONMENT OPERATIONS
#==============================================================================

def get_all_environments(fqdn: str, token: str, verify: bool = SSL_VERIFY) -> dict:
    """
    Get all environments registered in Fleet Management.
    
    :param fqdn: Fleet Management FQDN
    :param token: Auth token
    :param verify: SSL verification
    :return: Dict of {env_name: {'products': [product_ids]}}
    """
    if DEBUG:
        logger.debug("In: get_all_environments")
    
    url = f"https://{fqdn}/lcm/lcops/api/v2/environments"
    
    try:
        response = _make_request('GET', url, token, verify=verify)
        
        result = {}
        for environment in response:
            env_name = environment.get("environmentName", "")
            product_ids = [product['id'] for product in environment.get('products', [])]
            result[env_name] = {"products": product_ids}
        
        if DEBUG:
            logger.debug(f"Environments: {json.dumps(result, indent=2)}")
        
        return result
        
    except Exception as e:
        logger.error(f"Failed to get environments: {e}")
        return {}

def get_environment_id_by_name(fqdn: str, token: str, env_name: str, 
                                verify: bool = SSL_VERIFY) -> str:
    """
    Get the environment ID (vmid) for a given environment name.
    
    :param fqdn: Fleet Management FQDN
    :param token: Auth token
    :param env_name: Environment name to look up
    :param verify: SSL verification
    :return: Environment ID string or None if not found
    """
    if DEBUG:
        logger.debug(f"In: get_environment_id_by_name({env_name})")
    
    url = f"https://{fqdn}/lcm/lcops/api/v2/environments"
    
    try:
        response = _make_request('GET', url, token, verify=verify)
        
        for environment in response:
            if environment.get("environmentName") == env_name:
                return environment.get("environmentId")
        
        logger.warning(f"Environment not found: {env_name}")
        return None
        
    except Exception as e:
        logger.error(f"Failed to get environment ID: {e}")
        return None

#==============================================================================
# REQUEST STATUS TRACKING
#==============================================================================

def get_request_status(fqdn: str, token: str, request_id: str, 
                       verify: bool = SSL_VERIFY) -> str:
    """
    Get the status of a Fleet Management request.
    
    :param fqdn: Fleet Management FQDN
    :param token: Auth token
    :param request_id: Request ID to check
    :param verify: SSL verification
    :return: Status string (COMPLETED, FAILED, IN_PROGRESS, etc.)
    """
    if DEBUG:
        logger.debug(f"In: get_request_status({request_id})")
    
    url = f"https://{fqdn}/lcm/request/api/v2/requests/{request_id}"
    
    try:
        response = _make_request('GET', url, token, verify=verify)
        state = response.get("state", "UNKNOWN")
        
        if DEBUG:
            logger.debug(f"Request {request_id} state: {state}")
        
        return state
        
    except Exception as e:
        logger.error(f"Failed to get request status: {e}")
        return "FAILED"

def wait_for_request(fqdn: str, token: str, request_id: str, 
                     poll_interval: int = POWER_OP_POLL_INTERVAL,
                     max_wait: int = POWER_OP_MAX_WAIT,
                     verify: bool = SSL_VERIFY,
                     write_output=None) -> bool:
    """
    Wait for a Fleet Management request to complete.
    
    :param fqdn: Fleet Management FQDN
    :param token: Auth token
    :param request_id: Request ID to wait for
    :param poll_interval: Seconds between status checks
    :param max_wait: Maximum seconds to wait
    :param verify: SSL verification
    :param write_output: Optional logging function (lsf.write_output)
    :return: True if request completed successfully, False otherwise
    """
    start_time = time.time()
    check_count = 0
    
    while (time.time() - start_time) < max_wait:
        check_count += 1
        elapsed = int(time.time() - start_time)
        status = get_request_status(fqdn, token, request_id, verify)
        
        if write_output:
            write_output(f'  [Check {check_count}] Request {request_id[:8]}... status: {status} (elapsed: {elapsed}s)')
        else:
            print(f'INFO: [Check {check_count}] Request {request_id[:8]}... status: {status} (elapsed: {elapsed}s)')
        
        if status == "COMPLETED":
            if write_output:
                write_output(f'  Request completed successfully in {elapsed}s')
            return True
        elif status == "FAILED":
            if write_output:
                write_output(f'  Request FAILED after {elapsed}s')
            return False
        
        time.sleep(poll_interval)
    
    elapsed = int(time.time() - start_time)
    if write_output:
        write_output(f'  Request {request_id[:8]}... timed out after {elapsed}s (max: {max_wait}s)')
    else:
        print(f'WARNING: Request {request_id[:8]}... timed out after {elapsed}s')
    
    return False

#==============================================================================
# INVENTORY SYNC OPERATIONS
#==============================================================================

def trigger_inventory_sync_for_product(fqdn: str, token: str, env_id: str, 
                                       product_id: str, 
                                       verify: bool = SSL_VERIFY) -> str:
    """
    Trigger an inventory sync for a specific product in an environment.
    
    :param fqdn: Fleet Management FQDN
    :param token: Auth token
    :param env_id: Environment ID
    :param product_id: Product ID to sync
    :param verify: SSL verification
    :return: Request ID or None on failure
    """
    if DEBUG:
        logger.debug(f"In: trigger_inventory_sync_for_product({env_id}, {product_id})")
    
    url = f"https://{fqdn}/lcm/lcops/api/v2/environments/{env_id}/products/{product_id}/inventory-sync"
    
    try:
        response = _make_request('POST', url, token, payload={}, verify=verify)
        return response.get("requestId")
        
    except Exception as e:
        logger.error(f"Failed to trigger inventory sync: {e}")
        return None

def trigger_inventory_sync(fqdn: str, token: str, env_name: str, 
                          product_ids: list, verify: bool = SSL_VERIFY,
                          write_output=None) -> bool:
    """
    Trigger inventory sync for all products in an environment.
    
    :param fqdn: Fleet Management FQDN
    :param token: Auth token
    :param env_name: Environment name
    :param product_ids: List of product IDs to sync
    :param verify: SSL verification
    :param write_output: Optional logging function
    :return: True if all syncs succeeded, False otherwise
    """
    if write_output:
        write_output(f'Triggering inventory sync for {env_name}')
    else:
        print(f'TASK: Triggering inventory sync for {env_name}')
    
    env_id = get_environment_id_by_name(fqdn, token, env_name, verify)
    if not env_id:
        if write_output:
            write_output(f'Environment not found: {env_name}')
        return False
    
    all_success = True
    for product_id in product_ids:
        request_id = trigger_inventory_sync_for_product(fqdn, token, env_id, 
                                                        product_id, verify)
        if request_id:
            success = wait_for_request(fqdn, token, request_id,
                                       poll_interval=INVENTORY_SYNC_POLL_INTERVAL,
                                       max_wait=INVENTORY_SYNC_MAX_WAIT,
                                       verify=verify,
                                       write_output=write_output)
            if not success:
                all_success = False
        else:
            all_success = False
    
    return all_success

#==============================================================================
# POWER OPERATIONS
#==============================================================================

def power_state_product(fqdn: str, token: str, env_id: str, product_id: str,
                        power_state: str, verify: bool = SSL_VERIFY,
                        write_output=None) -> str:
    """
    Trigger a power state change for a product.
    
    :param fqdn: Fleet Management FQDN
    :param token: Auth token
    :param env_id: Environment ID
    :param product_id: Product ID
    :param power_state: Power state (power-on, power-off)
    :param verify: SSL verification
    :param write_output: Optional logging function
    :return: Request ID or None on failure
    """
    _log = write_output if write_output else lambda x: logger.error(x)
    
    if DEBUG:
        logger.debug(f"In: power_state_product({env_id}, {product_id}, {power_state})")
    
    url = f"https://{fqdn}/lcm/lcops/api/v2/environments/{env_id}/products/{product_id}/{power_state}"
    
    try:
        response = _make_request('POST', url, token, payload={}, verify=verify)
        return response.get("requestId")
        
    except requests.exceptions.HTTPError as e:
        error_detail = ""
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_json = e.response.json()
                error_detail = error_json.get('message', e.response.text)
            except:
                error_detail = e.response.text
        _log(f"HTTP Error triggering {power_state} for {product_id}: {e}")
        if error_detail:
            _log(f"  Detail: {error_detail}")
        return None
    except Exception as e:
        _log(f"Failed to trigger power state for {product_id}: {e}")
        return None

def trigger_power_event(fqdn: str, token: str, env_name: str, product_id: str,
                        power_state: str, verify: bool = SSL_VERIFY,
                        write_output=None, wait: bool = True) -> bool:
    """
    Trigger a power event for a product in an environment.
    
    :param fqdn: Fleet Management FQDN
    :param token: Auth token
    :param env_name: Environment name
    :param product_id: Product ID (vra, vrni, vrops, vrli, etc.)
    :param power_state: Power state (power-on, power-off)
    :param verify: SSL verification
    :param write_output: Optional logging function
    :param wait: Whether to wait for operation to complete
    :return: True if operation succeeded, False otherwise
    """
    _log = write_output if write_output else lambda x: print(f'INFO: {x}')
    
    _log(f'Getting environment ID for {env_name}')
    env_id = get_environment_id_by_name(fqdn, token, env_name, verify)
    
    if not env_id:
        _log(f'ERROR: Environment not found: {env_name}')
        return False
    
    _log(f'Triggering {power_state} for {product_id} in {env_name}')
    request_id = power_state_product(fqdn, token, env_id, product_id, 
                                     power_state, verify, write_output)
    
    if not request_id:
        _log(f'ERROR: Failed to trigger {power_state} for {product_id}')
        return False
    
    _log(f'Request ID: {request_id}')
    
    if wait:
        return wait_for_request(fqdn, token, request_id, verify=verify,
                               write_output=write_output)
    return True

def shutdown_products(fqdn: str, token: str, products: list, 
                      verify: bool = SSL_VERIFY,
                      write_output=None,
                      skip_inventory_sync: bool = False) -> bool:
    """
    Shutdown multiple products across all environments.
    
    This function will:
    1. Get all environments from Fleet Management
    2. Optionally trigger inventory sync for each environment
    3. Shutdown each product in the specified order
    
    :param fqdn: Fleet Management FQDN
    :param token: Auth token
    :param products: List of product IDs to shutdown (in order)
    :param verify: SSL verification
    :param write_output: Optional logging function
    :param skip_inventory_sync: Skip inventory sync (useful when vCenter is down)
    :return: True if all shutdowns succeeded, False otherwise
    """
    _log = write_output if write_output else lambda x: print(f'INFO: {x}')
    
    # Products that don't support power-off via Fleet Operations API
    unsupported_products = ['vrops', 'vrli']
    
    _log('Getting all environments from Fleet Management')
    env_list = get_all_environments(fqdn, token, verify)
    
    if not env_list:
        _log('No environments found in Fleet Management')
        return False
    
    _log(f'Found {len(env_list)} environment(s)')
    
    # Step 1: Sync inventory for all environments (optional)
    if not skip_inventory_sync:
        _log('Synchronizing inventory for all environments')
        _log('(Inventory sync may fail if vCenter is unavailable - this is expected during shutdown)')
        for env_name, details in env_list.items():
            product_ids = details.get('products', [])
            if product_ids:
                trigger_inventory_sync(fqdn, token, env_name, product_ids, 
                                      verify, write_output)
    else:
        _log('Skipping inventory sync')
    
    # Step 2: Shutdown products in order
    all_success = True
    for product in products:
        # Skip products that don't support power-off
        if product in unsupported_products:
            _log(f'Skipping {product} - power-off not supported via Fleet API (will be shut down via VM)')
            continue
            
        _log(f'Shutting down {product}...')
        
        product_found = False
        for env_name, details in env_list.items():
            if product in details.get('products', []):
                product_found = True
                success = trigger_power_event(fqdn, token, env_name, product,
                                             'power-off', verify, write_output)
                if not success:
                    _log(f'WARNING: Failed to shutdown {product} in {env_name}')
                    all_success = False
                break
        
        if not product_found:
            _log(f'{product} not found in any environment')
    
    return all_success

#==============================================================================
# STANDALONE TESTING
#==============================================================================

if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Fleet Management Operations')
    parser.add_argument('--fqdn', required=True, help='Fleet Management FQDN')
    parser.add_argument('--username', default='admin@local', help='Username')
    parser.add_argument('--password', required=True, help='Password')
    parser.add_argument('--action', choices=['list', 'shutdown'], default='list',
                        help='Action to perform')
    parser.add_argument('--products', nargs='+', default=['vra', 'vrni'],
                        help='Products to shutdown')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    
    args = parser.parse_args()
    
    if args.debug:
        DEBUG = True
        logging.basicConfig(level=logging.DEBUG)
    
    token = get_encoded_token(args.username, args.password)
    
    if args.action == 'list':
        envs = get_all_environments(args.fqdn, token)
        print(json.dumps(envs, indent=2))
    
    elif args.action == 'shutdown':
        success = shutdown_products(args.fqdn, token, args.products)
        print(f'Shutdown {"succeeded" if success else "failed"}')
        sys.exit(0 if success else 1)

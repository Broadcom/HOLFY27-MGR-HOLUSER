#!/usr/bin/env python3
# fleet.py - HOLFY27 Fleet Management (SDDC Manager) Operations
# Version 2.0 - February 2026
# Author - Burke Azbill and HOL Core Team
# Provides Fleet Operations API integration for shutdown orchestration
#
# v2.0 Changes:
# - Added VCF 9.1 Fleet LCM plugin API support (ops-a proxy, JWT Bearer auth,
#   component-based shutdown with task polling)
# - Retained all VCF 9.0 legacy API functions (opslcm-a, Basic auth,
#   environment/product-based power operations)
# - Added detect_vcf_version() for config-based version selection
# - Added probe_vcf_91() for runtime auto-detection of VCF 9.1 API
# - Updated standalone CLI with --version flag for testing both API paths

"""
Fleet Management (SDDC Manager) API Integration Module

This module provides functions for interacting with VMware Fleet Operations
(via SDDC Manager/VCF Operations Manager) to orchestrate graceful
shutdown of VCF environments.

Supports two API versions:
  VCF 9.0 (Legacy):
    - Endpoint: opslcm-a (SDDC Manager LCM)
    - Auth: Basic (base64 encoded credentials)
    - API: /lcm/lcops/api/v2/environments/{envId}/products/{productId}/power-off
    - Products: vra, vrni, vrops, vrli, vrlcm

  VCF 9.1 (Fleet LCM Plugin):
    - Endpoint: ops-a (VCF Operations Manager proxy)
    - Auth: JWT Bearer token via suite-api
    - API: /vcf-operations/plug/fleet-lcm/v1/components/{componentId}?action=shutdown
    - Components: VCFA (VCF Automation), VRNI (Operations for Networks), etc.

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

# Timeouts and retries (VCF 9.0)
REQUEST_TIMEOUT = 30  # seconds for API requests
POWER_OP_POLL_INTERVAL = 45  # seconds between status checks
POWER_OP_MAX_WAIT = 1800  # 30 minutes max wait for power operation
INVENTORY_SYNC_POLL_INTERVAL = 15  # seconds between inventory sync checks
INVENTORY_SYNC_MAX_WAIT = 300  # 5 minutes max wait for inventory sync

# VCF 9.1 Fleet LCM Plugin configuration
V91_API_BASE = '/vcf-operations/plug/fleet-lcm/v1'
V91_TASK_POLL_INTERVAL = 15  # seconds between task status checks
V91_TASK_MAX_WAIT = 1800     # 30 minutes max wait for shutdown workflow
V91_TOKEN_TIMEOUT = 30       # seconds for suite-api token acquisition

# VCF 9.0 product name -> VCF 9.1 component type mapping
PRODUCT_TO_COMPONENT_TYPE = {
    'vra':   'VCFA',
    'vrni':  'VRNI',
    'vrops': 'VROPS',
    'vrli':  'VRLI',
    'vrlcm': 'VRLCM',
}

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
# VCF 9.1 - JWT TOKEN MANAGEMENT
#==============================================================================

def get_ops_jwt_token(ops_fqdn: str, username: str, password: str,
                      verify: bool = SSL_VERIFY) -> str:
    """
    Obtain a JWT Bearer token from VCF Operations Manager suite-api.
    
    The suite-api /auth/token/acquire endpoint returns an ops token that is
    accepted by the Fleet LCM plugin proxy as a Bearer token.
    
    :param ops_fqdn: VCF Operations Manager FQDN (e.g., ops-a.site-a.vcf.lab)
    :param username: Local admin username (e.g., admin)
    :param password: Admin password
    :param verify: SSL verification flag
    :return: JWT token string
    :raises: Exception on authentication failure
    """
    url = f'https://{ops_fqdn}/suite-api/api/auth/token/acquire'
    payload = {
        'username': username,
        'password': password,
        'authSource': 'local'
    }
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json'
    }

    try:
        response = requests.post(url, json=payload, headers=headers,
                                 verify=verify, timeout=V91_TOKEN_TIMEOUT)
        response.raise_for_status()
        token = response.json().get('token')
        if not token:
            raise ValueError('No token in suite-api response')
        return token
    except requests.exceptions.HTTPError as e:
        logger.error(f"Failed to acquire ops JWT token: {e}")
        raise
    except Exception as e:
        logger.error(f"JWT token acquisition error: {e}")
        raise

#==============================================================================
# VCF 9.1 - API HELPERS
#==============================================================================

def _make_v91_request(method: str, ops_fqdn: str, path: str, token: str,
                      payload: dict = None, verify: bool = SSL_VERIFY,
                      params: dict = None) -> dict:
    """
    Make an authenticated API request to the VCF 9.1 Fleet LCM plugin.
    
    :param method: HTTP method (GET, POST)
    :param ops_fqdn: VCF Operations Manager FQDN
    :param path: API path (appended to V91_API_BASE)
    :param token: JWT Bearer token
    :param payload: Optional request body (dict)
    :param verify: SSL verification flag
    :param params: Optional query parameters
    :return: JSON response as dict
    :raises: requests.HTTPError on failure
    """
    url = f'https://{ops_fqdn}{V91_API_BASE}{path}'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json'
    }

    try:
        if method.upper() == 'GET':
            response = requests.get(url, headers=headers, params=params,
                                    verify=verify, timeout=REQUEST_TIMEOUT)
        elif method.upper() == 'POST':
            data = json.dumps(payload) if payload else None
            response = requests.post(url, headers=headers, data=data,
                                     params=params, verify=verify,
                                     timeout=REQUEST_TIMEOUT)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        response.raise_for_status()
        if not response.text:
            return {}
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as e:
            content_type = response.headers.get('Content-Type', '')
            logger.error(f"V91 API returned non-JSON response "
                         f"(Content-Type: {content_type}, "
                         f"length: {len(response.text)}): {e}")
            logger.debug(f"Response body (first 500 chars): "
                         f"{response.text[:500]}")
            raise

    except requests.exceptions.HTTPError as e:
        logger.error(f"V91 HTTP Error: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.debug(f"Response: {e.response.text}")
        raise
    except requests.exceptions.ConnectionError as e:
        logger.error(f"V91 Connection Error: {e}")
        raise
    except requests.exceptions.Timeout as e:
        logger.error(f"V91 Timeout Error: {e}")
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"V91 Request Error: {e}")
        raise

#==============================================================================
# VCF 9.1 - COMPONENT OPERATIONS
#==============================================================================

def get_components_v91(ops_fqdn: str, token: str,
                       verify: bool = SSL_VERIFY) -> list:
    """
    List all components registered in the VCF 9.1 Fleet LCM plugin.
    
    :param ops_fqdn: VCF Operations Manager FQDN
    :param token: JWT Bearer token
    :param verify: SSL verification flag
    :return: List of component dicts with id, componentType, fqdn, status, etc.
    """
    if DEBUG:
        logger.debug("In: get_components_v91")

    try:
        response = _make_v91_request('GET', ops_fqdn, '/components', token,
                                     verify=verify)
        components = response if isinstance(response, list) else response.get('content', [])

        if DEBUG:
            logger.debug(f"Components: {json.dumps(components, indent=2)}")

        return components

    except Exception as e:
        logger.error(f"Failed to get V91 components: {e}")
        return []

def find_component_by_type(components: list, component_type: str) -> dict:
    """
    Find a component in the list by its componentType.
    
    :param components: List of component dicts from get_components_v91()
    :param component_type: Component type to match (e.g., VCFA, VRNI)
    :return: Matching component dict or None
    """
    for comp in components:
        if comp.get('componentType', '').upper() == component_type.upper():
            return comp
    return None

#==============================================================================
# VCF 9.1 - SHUTDOWN OPERATIONS
#==============================================================================

def shutdown_component_v91(ops_fqdn: str, token: str, component_id: str,
                           verify: bool = SSL_VERIFY,
                           write_output=None) -> str:
    """
    Trigger a graceful shutdown of a VCF 9.1 component via Fleet LCM plugin.
    
    Sends POST /components/{componentId}?action=shutdown which initiates a
    SHUTDOWN_COMPONENT_WORKFLOW and returns a task object with an ID that can
    be polled for completion.
    
    :param ops_fqdn: VCF Operations Manager FQDN
    :param token: JWT Bearer token
    :param component_id: Component UUID to shut down
    :param verify: SSL verification flag
    :param write_output: Optional logging function
    :return: Task ID string or None on failure
    """
    _log = write_output if write_output else lambda x: logger.info(x)

    try:
        response = _make_v91_request('POST', ops_fqdn,
                                     f'/components/{component_id}',
                                     token, verify=verify,
                                     params={'action': 'shutdown'})
        task_id = response.get('id')
        task_name = response.get('name', 'UNKNOWN')
        status = response.get('status', 'UNKNOWN')
        desc = response.get('description', {}).get('defaultMessage', '')

        if task_id:
            _log(f'  Shutdown workflow started: {task_name} (task: {task_id[:8]}...)')
            if desc:
                _log(f'  Description: {desc}')
            _log(f'  Initial status: {status}')

        return task_id

    except requests.exceptions.HTTPError as e:
        error_detail = ""
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_json = e.response.json()
                error_detail = error_json.get('message', e.response.text)
            except Exception:
                error_detail = e.response.text
        _log(f'  HTTP Error triggering shutdown for component {component_id}: {e}')
        if error_detail:
            _log(f'  Detail: {error_detail}')
        return None
    except Exception as e:
        _log(f'  Failed to trigger shutdown for component {component_id}: {e}')
        return None

#==============================================================================
# VCF 9.1 - TASK STATUS TRACKING
#==============================================================================

def get_task_status_v91(ops_fqdn: str, token: str, task_id: str,
                        verify: bool = SSL_VERIFY) -> dict:
    """
    Get the status of a VCF 9.1 Fleet LCM task.
    
    :param ops_fqdn: VCF Operations Manager FQDN
    :param token: JWT Bearer token
    :param task_id: Task UUID to check
    :param verify: SSL verification flag
    :return: Dict with 'status' key (RUNNING, SUCCEEDED, FAILED) and 'stages'
    """
    if DEBUG:
        logger.debug(f"In: get_task_status_v91({task_id})")

    try:
        response = _make_v91_request('GET', ops_fqdn, f'/tasks/{task_id}',
                                     token, verify=verify)
        return response
    except Exception as e:
        logger.error(f"Failed to get V91 task status: {e}")
        return {'status': 'FAILED', 'error': str(e)}

def wait_for_task_v91(ops_fqdn: str, token: str, task_id: str,
                      poll_interval: int = V91_TASK_POLL_INTERVAL,
                      max_wait: int = V91_TASK_MAX_WAIT,
                      verify: bool = SSL_VERIFY,
                      write_output=None) -> bool:
    """
    Wait for a VCF 9.1 Fleet LCM task to complete.
    
    :param ops_fqdn: VCF Operations Manager FQDN
    :param token: JWT Bearer token
    :param task_id: Task UUID to wait for
    :param poll_interval: Seconds between status checks
    :param max_wait: Maximum seconds to wait
    :param verify: SSL verification flag
    :param write_output: Optional logging function
    :return: True if task succeeded, False otherwise
    """
    _log = write_output if write_output else lambda x: print(f'INFO: {x}')
    start_time = time.time()
    check_count = 0

    while (time.time() - start_time) < max_wait:
        check_count += 1
        elapsed = int(time.time() - start_time)
        task_info = get_task_status_v91(ops_fqdn, token, task_id, verify)
        status = task_info.get('status', 'UNKNOWN')

        stages = task_info.get('stages', [])
        stage_summary = ''
        if stages:
            current_stage = stages[-1]
            stage_name = current_stage.get('name', '')
            stage_status = current_stage.get('status', '')
            stage_summary = f' | stage: {stage_name}={stage_status}'

        _log(f'  [Check {check_count}] Task {task_id[:8]}... status: {status}{stage_summary} (elapsed: {elapsed}s)')

        if status == 'SUCCEEDED':
            _log(f'  Task completed successfully in {elapsed}s')
            return True
        elif status == 'FAILED':
            messages = task_info.get('messages', [])
            if messages:
                for msg in messages[:3]:
                    msg_text = msg if isinstance(msg, str) else msg.get('defaultMessage', str(msg))
                    _log(f'  Error: {msg_text}')
            _log(f'  Task FAILED after {elapsed}s')
            return False

        time.sleep(poll_interval)

    elapsed = int(time.time() - start_time)
    _log(f'  Task {task_id[:8]}... timed out after {elapsed}s (max: {max_wait}s)')
    return False

#==============================================================================
# VCF 9.1 - TOP-LEVEL SHUTDOWN
#==============================================================================

def shutdown_products_v91(ops_fqdn: str, token: str, products: list,
                          verify: bool = SSL_VERIFY,
                          write_output=None) -> bool:
    """
    Shutdown multiple products via the VCF 9.1 Fleet LCM plugin API.
    
    This function will:
    1. List all components from Fleet LCM
    2. Match requested products to component types
    3. Shutdown each matching component
    4. Poll task status until completion
    
    :param ops_fqdn: VCF Operations Manager FQDN
    :param token: JWT Bearer token
    :param products: List of product IDs to shutdown (vra, vrni, etc.)
    :param verify: SSL verification flag
    :param write_output: Optional logging function
    :return: True if all shutdowns succeeded, False otherwise
    """
    _log = write_output if write_output else lambda x: print(f'INFO: {x}')

    _log('Getting all components from Fleet LCM (VCF 9.1)')
    components = get_components_v91(ops_fqdn, token, verify)

    if not components:
        _log('No components found in Fleet LCM')
        return False

    _log(f'Found {len(components)} component(s):')
    for comp in components:
        comp_type = comp.get('componentType', 'UNKNOWN')
        comp_fqdn = comp.get('fqdn', comp.get('hostname', 'unknown'))
        comp_status = comp.get('status', comp.get('powerState', 'unknown'))
        _log(f'  {comp_type}: {comp_fqdn} (status: {comp_status})')

    all_success = True
    for product in products:
        component_type = PRODUCT_TO_COMPONENT_TYPE.get(product)
        if not component_type:
            _log(f'Skipping {product} - no VCF 9.1 component type mapping')
            continue

        comp = find_component_by_type(components, component_type)
        if not comp:
            _log(f'{product} ({component_type}) not found in Fleet LCM components')
            continue

        comp_id = comp.get('id', comp.get('componentId', ''))
        comp_fqdn = comp.get('fqdn', comp.get('hostname', 'unknown'))

        _log(f'Shutting down {product} ({component_type}): {comp_fqdn}...')
        task_id = shutdown_component_v91(ops_fqdn, token, comp_id, verify,
                                         write_output)
        if not task_id:
            _log(f'WARNING: Failed to trigger shutdown for {product}')
            all_success = False
            continue

        success = wait_for_task_v91(ops_fqdn, token, task_id, verify=verify,
                                    write_output=write_output)
        if not success:
            _log(f'WARNING: Shutdown workflow for {product} did not complete successfully')
            all_success = False

    return all_success

#==============================================================================
# VERSION DETECTION
#==============================================================================

def detect_vcf_version(config) -> str:
    """
    Detect the VCF version from the config file.
    
    Checks [VCF] vcf_version first, then [SHUTDOWN] vcf_version.
    Returns None if not configured (caller should auto-probe).
    
    :param config: ConfigParser object
    :return: Version string (e.g., '9.0', '9.1') or None
    """
    for section in ('VCF', 'SHUTDOWN'):
        if config.has_option(section, 'vcf_version'):
            version = config.get(section, 'vcf_version').strip()
            if version:
                return version
    return None

def probe_vcf_91(ops_fqdn: str, verify: bool = SSL_VERIFY) -> bool:
    """
    Probe whether the VCF 9.1 Fleet LCM plugin API is available.
    
    Attempts a lightweight GET request to the tasks endpoint. If the API
    responds (even with an auth error 401), the plugin is present. A
    connection error or 404 indicates VCF 9.0 or the plugin is unavailable.
    
    :param ops_fqdn: VCF Operations Manager FQDN (e.g., ops-a.site-a.vcf.lab)
    :param verify: SSL verification flag
    :return: True if VCF 9.1 Fleet LCM plugin is detected
    """
    url = f'https://{ops_fqdn}{V91_API_BASE}/tasks'
    try:
        response = requests.get(url, params={'pageSize': '1'},
                                verify=verify, timeout=10,
                                headers={'Accept': 'application/json'})
        # 200 = API available, 401 = API present but needs auth
        # 404 = plugin not installed (VCF 9.0)
        if response.status_code in (200, 401, 403):
            return True
        return False
    except requests.exceptions.ConnectionError:
        return False
    except requests.exceptions.Timeout:
        return False
    except Exception:
        return False

#==============================================================================
# STANDALONE TESTING
#==============================================================================

if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Fleet Management Operations')
    parser.add_argument('--fqdn', required=True,
                        help='Fleet Management FQDN (opslcm-a for 9.0, ops-a for 9.1)')
    parser.add_argument('--username', default='admin@local',
                        help='Username (9.0: admin@local, 9.1: admin)')
    parser.add_argument('--password', required=True, help='Password')
    parser.add_argument('--action', choices=['list', 'shutdown', 'probe'],
                        default='list', help='Action to perform')
    parser.add_argument('--products', nargs='+', default=['vra', 'vrni'],
                        help='Products to shutdown')
    parser.add_argument('--version', choices=['9.0', '9.1'], default='9.0',
                        help='VCF API version (9.0=legacy, 9.1=Fleet LCM plugin)')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    
    args = parser.parse_args()
    
    if args.debug:
        DEBUG = True
        logging.basicConfig(level=logging.DEBUG)

    if args.action == 'probe':
        is_91 = probe_vcf_91(args.fqdn)
        print(f'VCF 9.1 Fleet LCM plugin detected: {is_91}')
        sys.exit(0)

    if args.version == '9.1':
        # VCF 9.1 API path
        username = args.username.replace('@local', '') if '@' in args.username else args.username
        print(f'Authenticating to {args.fqdn} via suite-api as {username}...')
        token = get_ops_jwt_token(args.fqdn, username, args.password)
        print('JWT token acquired')

        if args.action == 'list':
            components = get_components_v91(args.fqdn, token)
            print(json.dumps(components, indent=2))
        elif args.action == 'shutdown':
            success = shutdown_products_v91(args.fqdn, token, args.products)
            print(f'Shutdown {"succeeded" if success else "failed"}')
            sys.exit(0 if success else 1)
    else:
        # VCF 9.0 API path (existing behavior)
        token = get_encoded_token(args.username, args.password)

        if args.action == 'list':
            envs = get_all_environments(args.fqdn, token)
            print(json.dumps(envs, indent=2))
        elif args.action == 'shutdown':
            success = shutdown_products(args.fqdn, token, args.products)
            print(f'Shutdown {"succeeded" if success else "failed"}')
            sys.exit(0 if success else 1)

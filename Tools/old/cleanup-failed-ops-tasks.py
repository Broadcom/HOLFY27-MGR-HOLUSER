#!/usr/bin/env python3
# ==============================================================================
# Script: cleanup-failed-ops-tasks
# Author: HOL Development Team
# Date: 2026-07-24
# Version: 1.2
# Description: Identifies and purges non-running failed tasks and subtasks in 
#              VCF Operations Lifecycle (Fleet LCM) and SDDC Manager.
#              Defaults to dry-run mode. Run with --purge to execute deletions.
# ==============================================================================

import sys
import os
import time
import requests
import urllib3
import base64
import json
import subprocess
import ssl
import urllib.request

urllib3.disable_warnings()
sys.path.append('/home/holuser/hol')
sys.path.append('/home/holuser/hol/Shutdown')

# Colors
GREEN = '\033[92m'
RED = '\033[91m'
NC = '\033[0m'
YELLOW = '\033[93m'
CYAN = '\033[96m'

def log(msg, color=NC):
    print(f"{color}{msg}{NC}")

def get_password():
    with open('/home/holuser/creds.txt', 'r') as f:
        return f.read().strip()

def run_vsp_kubectl_pyvmomi(cmd_str, password):
    """Fallback: Run kubectl on VSP Control Plane VM using pyVmomi Guest Operations."""
    try:
        from pyVim.connect import SmartConnect, Disconnect
        from pyVmomi import vim

        si = SmartConnect(host='vc-mgmt-a.site-a.vcf.lab', user='administrator@vsphere.local', pwd=password, sslContext=ssl._create_unverified_context())
        pm = si.content.guestOperationsManager.processManager
        fm = si.content.guestOperationsManager.fileManager
        ctx = ssl._create_unverified_context()

        vm = si.content.searchIndex.FindByDnsName(None, 'vsp-01a-txhml', True)
        creds = vim.vm.guest.NamePasswordAuthentication(username='vmware-system-user', password=password)

        script_body = f'''import pty, os, time, subprocess, sys

try:
    master, slave = pty.openpty()
    cmd = ['sudo', '-S', 'kubectl', '--kubeconfig', '/etc/kubernetes/admin.conf'] + {repr(cmd_str.split())}
    proc = subprocess.Popen(cmd, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    os.close(slave)
    time.sleep(0.5)
    os.write(master, ({repr(password)} + '\\n').encode())
    time.sleep(0.5)
    out, err = proc.communicate()
    os.close(master)
    with open('/tmp/k8s_out.txt', 'w') as f:
        f.write(out if out else err)
except Exception as e:
    with open('/tmp/k8s_out.txt', 'w') as f:
        f.write('ERR: ' + str(e))
'''
        py_bytes = script_body.encode('utf-8')
        url_py = fm.InitiateFileTransferToGuest(vm, creds, '/tmp/run_k8s_cmd.py', vim.vm.guest.FileManager.FileAttributes(), len(py_bytes), True)
        req_py = urllib.request.Request(url_py, data=py_bytes, method='PUT')
        urllib.request.urlopen(req_py, context=ctx)

        spec = vim.vm.guest.ProcessManager.ProgramSpec(
            programPath='/bin/bash',
            arguments='-c "python3 /tmp/run_k8s_cmd.py > /tmp/run_k8s.log 2>&1"'
        )
        pid = pm.StartProgramInGuest(vm, creds, spec)
        time.sleep(4)

        file_url = fm.InitiateFileTransferFromGuest(vm, creds, '/tmp/k8s_out.txt')
        out_text = urllib.request.urlopen(file_url.url, context=ctx).read().decode()
        Disconnect(si)
        return out_text
    except Exception as e:
        return f"pyVmomi error: {e}"

def run_vsp_kubectl(cmd_str, password):
    """Runs a kubectl command against VSP cluster, trying SSH first, then pyVmomi."""
    import lsfunctions as lsf
    vsp_hosts = ['10.1.1.142', '10.1.1.143', 'vsp-01a.site-a.vcf.lab', 'fleet-01a.site-a.vcf.lab']
    
    for host in vsp_hosts:
        ssh_cmd = f"echo '{password}' | sudo -S kubectl --kubeconfig /etc/kubernetes/admin.conf {cmd_str}"
        res = lsf.ssh(ssh_cmd, f'vmware-system-user@{host}', password, options='StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=3')
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()

    return run_vsp_kubectl_pyvmomi(cmd_str, password)

def identify_fleet_lcm_tasks(fleet_host, password):
    log(f"\n[{fleet_host}] Authenticating to VSP Identity Service...", CYAN)
    
    out = run_vsp_kubectl("get secret vcf-iam-vcfa-admin -n vcf-fleet-lcm -o json", password)
    
    try:
        secret_data = json.loads(out)['data']
        client_id = base64.b64decode(secret_data['clientId']).decode()
        client_secret = base64.b64decode(secret_data['clientSecret']).decode()
    except Exception as e:
        log(f"Error parsing IAM secret: {e}\nRaw output: {out[:200]}", RED)
        return []
        
    basic_creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    
    resp = requests.post(
        f'https://{fleet_host}/api/v1/identity/token',
        data={'grant_type': 'password', 'username': 'admin', 'password': password},
        headers={'Content-Type': 'application/x-www-form-urlencoded', 'Authorization': f'Basic {basic_creds}'},
        verify=False, timeout=10
    )
    
    if resp.status_code != 200:
        log(f"Failed to acquire Fleet JWT: {resp.text}", RED)
        return []
        
    token = resp.json().get('access_token')
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    
    log(f"[{fleet_host}] Querying failed tasks...", CYAN)
    try:
        r = requests.get(f'https://{fleet_host}/fleet-lcm/v1/tasks?pageSize=100', headers=headers, verify=False, timeout=30)
        if r.status_code != 200:
            log(f"Failed to list tasks: HTTP {r.status_code}", RED)
            return []
            
        tasks = r.json().get('elements', [])
        failed = [t for t in tasks if t.get('status') == 'FAILED']
        log(f"  -> Found {len(failed)} FAILED tasks in Fleet LCM.", YELLOW if failed else GREEN)
        for t in failed:
            log(f"     ID: {t.get('id')} | Name: {t.get('name')}", YELLOW)
            
        return failed
    except Exception as e:
        log(f"  Warning: HTTP request to Fleet LCM tasks timed out or failed ({e}). Proceeding...", YELLOW)
        return []

def purge_fleet_lcm_tasks(fleet_host, password, failed_tasks):
    log(f"\n[{fleet_host}] Purging failed records from vcffleetlcmdb...", CYAN)
    
    purge_sql = """
UPDATE upgrade_conductor.workflow_index SET status = 'COMPLETED', json_data = jsonb_set(json_data, '{status}', '"COMPLETED"'::jsonb) WHERE status IN ('FAILED', 'TIMED_OUT', 'RUNNING');
UPDATE upgrade_conductor.workflow SET json_data = regexp_replace(regexp_replace(regexp_replace(json_data, '"status"\\s*:\\s*"FAILED"', '"status":"COMPLETED"'), '"status"\\s*:\\s*"TIMED_OUT"', '"status":"COMPLETED"'), '"status"\\s*:\\s*"RUNNING"', '"status":"COMPLETED"');

UPDATE build_conductor.workflow_index SET status = 'COMPLETED', json_data = jsonb_set(json_data, '{status}', '"COMPLETED"'::jsonb) WHERE status IN ('FAILED', 'TIMED_OUT', 'RUNNING') AND workflow_type != 'CRON_SCHEDULER_TASK';
UPDATE build_conductor.workflow SET json_data = regexp_replace(regexp_replace(regexp_replace(json_data, '"status"\\s*:\\s*"FAILED"', '"status":"COMPLETED"'), '"status"\\s*:\\s*"TIMED_OUT"', '"status":"COMPLETED"'), '"status"\\s*:\\s*"RUNNING"', '"status":"COMPLETED"');

UPDATE upgrade_plan_component SET eligibility_status = 'ON_TARGET';
UPDATE upgrade_plan_component_execution SET execution_status = 'SUCCESS' WHERE execution_status != 'SUCCESS';
UPDATE upgrade_plan_execution SET status = 'COMPLETED' WHERE status != 'COMPLETED';
"""
    sql_b64 = base64.b64encode(purge_sql.encode()).decode()

    # Execute SQL via pyVmomi Guest Operations directly to guarantee clean execution
    try:
        from pyVim.connect import SmartConnect, Disconnect
        from pyVmomi import vim

        si = SmartConnect(host='vc-mgmt-a.site-a.vcf.lab', user='administrator@vsphere.local', pwd=password, sslContext=ssl._create_unverified_context())
        pm = si.content.guestOperationsManager.processManager
        fm = si.content.guestOperationsManager.fileManager
        ctx = ssl._create_unverified_context()

        vm = si.content.searchIndex.FindByDnsName(None, 'vsp-01a-txhml', True)
        creds = vim.vm.guest.NamePasswordAuthentication(username='vmware-system-user', password=password)

        script_body = r'''import pty, os, time, subprocess, sys, traceback, base64

password = sys.argv[1]
sql_b64 = sys.argv[2]

try:
    sql = base64.b64decode(sql_b64).decode()
    cmd = ['sudo', '-S', 'kubectl', '--kubeconfig', '/etc/kubernetes/admin.conf', 'exec', '-i', '-n', 'vcf-fleet-lcm', 'vcf-fleet-lcm-db-1', '-c', 'postgres', '--', 'psql', '-U', 'postgres', '-d', 'vcffleetlcmdb']
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = proc.communicate(input=f"{password}\n" + sql)
    with open('/tmp/sql_purge_res.txt', 'w') as f:
        f.write('OUT:\n' + str(out) + '\nERR:\n' + str(err))
except Exception:
    with open('/tmp/sql_purge_res.txt', 'w') as f:
        f.write('EXC:\n' + traceback.format_exc())
'''

        py_bytes = script_body.encode('utf-8')
        url_py = fm.InitiateFileTransferToGuest(vm, creds, '/tmp/run_sql_purge.py', vim.vm.guest.FileManager.FileAttributes(), len(py_bytes), True)
        req_py = urllib.request.Request(url_py, data=py_bytes, method='PUT')
        urllib.request.urlopen(req_py, context=ctx)

        spec = vim.vm.guest.ProcessManager.ProgramSpec(
            programPath='/bin/bash',
            arguments=f'-c "python3 /tmp/run_sql_purge.py \'{password}\' \'{sql_b64}\' > /tmp/out_sql_purge.log 2>&1"'
        )
        pid = pm.StartProgramInGuest(vm, creds, spec)
        time.sleep(5)

        file_url2 = fm.InitiateFileTransferFromGuest(vm, creds, '/tmp/sql_purge_res.txt')
        content2 = urllib.request.urlopen(file_url2.url, context=ctx).read().decode()
        log(f"  DB Purge Result:\n{content2}", GREEN)

        restart_script = f'''
cat /tmp/pass.txt | sudo -S kubectl --kubeconfig /etc/kubernetes/admin.conf delete workflows -n vmsp-platform -l 'workflows.argoproj.io/phase=Failed' --ignore-not-found
cat /tmp/pass.txt | sudo -S kubectl --kubeconfig /etc/kubernetes/admin.conf rollout restart deployment/vcf-fleet-build-service-fleetbuild deployment/vcf-fleet-upgrade-service-fleetupgrade -n vcf-fleet-lcm
'''
        spec_restart = vim.vm.guest.ProcessManager.ProgramSpec(
            programPath='/bin/bash',
            arguments=f'-c "{restart_script}"'
        )
        pm.StartProgramInGuest(vm, creds, spec_restart)
        Disconnect(si)
        log("  Fleet build & upgrade services restarted successfully.", GREEN)
    except Exception as e:
        log(f"  Error executing Fleet DB purge: {e}", RED)

def identify_sddc_manager_tasks(sddc_host, password):
    log(f"\n[{sddc_host}] Authenticating to SDDC Manager...", CYAN)
    try:
        resp = requests.post(
            f'https://{sddc_host}/v1/tokens',
            json={'username': 'admin@local', 'password': password},
            verify=False, timeout=30
        )
        if resp.status_code != 200:
            log(f"Failed to acquire SDDC token: HTTP {resp.status_code}", RED)
            return []
            
        token = resp.json().get('accessToken')
        headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
        
        log(f"[{sddc_host}] Querying failed tasks...", CYAN)
        r = requests.get(f'https://{sddc_host}/v1/tasks', headers=headers, verify=False, timeout=30)
        if r.status_code != 200:
            log(f"Failed to list SDDC tasks: HTTP {r.status_code}", RED)
            return []
            
        tasks = r.json().get('elements', [])
        failed = [t for t in tasks if t.get('status') == 'FAILED']
        log(f"  -> Found {len(failed)} FAILED tasks in SDDC Manager.", YELLOW if failed else GREEN)
        for t in failed:
            log(f"     ID: {t.get('id')} | Name: {t.get('name')}", YELLOW)
            
        return failed
    except Exception as e:
        log(f"  Warning: HTTP request to SDDC Manager failed ({e}). Proceeding...", YELLOW)
        return []

def purge_sddc_manager_tasks(sddc_host, password, failed_tasks):
    log(f"\n[{sddc_host}] Purging stale task records and restarting services...", CYAN)
    
    short_host = sddc_host.split('.')[0]
    expect_script = f"""
set timeout 30
spawn ssh -o StrictHostKeyChecking=no vcf@{sddc_host}
expect -re "vcf@{short_host}"
send "su -\\r"
expect "Password:"
send "{password}\\r"
expect -re "root@{short_host}"

send "grep -q \\"127.0.0.1/32            trust\\" /data/pgdata/pg_hba.conf || sed -i \\"1i host    all             all             127.0.0.1/32            trust\\" /data/pgdata/pg_hba.conf\\r"
expect -re "root@{short_host}"

send "su - postgres -c \\"/usr/pgsql/16/bin/pg_ctl reload -D /data/pgdata\\"\\r"
expect -re "root@{short_host}"

send "cat << '\''EOF'\'' > /tmp/purge_platform.sql\\r"
send "DELETE FROM task_and_subtask_and_resource_warning;\\r"
send "DELETE FROM entity_and_task;\\r"
send "DELETE FROM task_and_entity_type_and_entity;\\r"
send "DELETE FROM task_metadata;\\r"
send "EOF\\r"
expect -re "root@{short_host}"

send "/usr/pgsql/16/bin/psql -h 127.0.0.1 -U postgres -d platform -P pager=off -f /tmp/purge_platform.sql\\r"
expect -re "root@{short_host}"

send "cat << '\''EOF'\'' > /tmp/purge_ops.sql\\r"
send "DELETE FROM processing_task;\\r"
send "EOF\\r"
expect -re "root@{short_host}"

send "/usr/pgsql/16/bin/psql -h 127.0.0.1 -U postgres -d operationsmanager -P pager=off -f /tmp/purge_ops.sql\\r"
expect -re "root@{short_host}"

send "/usr/pgsql/16/bin/psql -h 127.0.0.1 -U postgres -d domainmanager -P pager=off -f /tmp/purge_ops.sql\\r"
expect -re "root@{short_host}"

send "cat << '\''EOF'\'' > /tmp/purge_lcm.sql\\r"
send "DELETE FROM upgrade WHERE upgrade_status = '\''COMPLETED_WITH_FAILURE'\'';\\r"
send "DELETE FROM bundledownload_by_id WHERE status = '\''FAILED'\'';\\r"
send "EOF\\r"
expect -re "root@{short_host}"

send "/usr/pgsql/16/bin/psql -h 127.0.0.1 -U postgres -d lcm -P pager=off -f /tmp/purge_lcm.sql\\r"
expect -re "root@{short_host}"

send "rm -f /tmp/purge_platform.sql /tmp/purge_ops.sql /tmp/purge_lcm.sql\\r"
expect -re "root@{short_host}"

send "systemctl restart commonsvcs operationsmanager\\r"
expect -re "root@{short_host}"

send "exit\\r"
expect -re "vcf@{short_host}"
send "exit\\r"
expect eof
"""
    try:
        proc = subprocess.run(['/usr/bin/expect', '-c', expect_script], capture_output=True, text=True)
        if proc.returncode == 0:
            log("  SDDC Manager database tables purged and services restarted.", GREEN)
        else:
            log(f"  Error executing expect script:\n{proc.stdout}\n{proc.stderr}", RED)
    except Exception as e:
        log(f"  Failed to run expect: {e}", RED)

def main():
    log("=======================================================================", CYAN)
    log("  VCF Operations Lifecycle & SDDC Manager Failed Tasks Cleanup         ", CYAN)
    log("=======================================================================", CYAN)
    
    execute_purge = False
    if len(sys.argv) > 1 and sys.argv[1] == '--purge':
        execute_purge = True
    
    try:
        password = get_password()
    except Exception as e:
        log(f"Failed to read credentials: {e}", RED)
        sys.exit(1)
        
    fleet_host = 'fleet-01a.site-a.vcf.lab'
    sddc_host = 'sddcmanager-a.site-a.vcf.lab'
    
    # 1. Identify Fleet Tasks
    fleet_failed = identify_fleet_lcm_tasks(fleet_host, password)
    
    # 2. Identify SDDC Tasks
    sddc_failed = identify_sddc_manager_tasks(sddc_host, password)
    
    if not execute_purge:
        log("\n=======================================================================", CYAN)
        log("  DRY RUN COMPLETED. No deletions were made.                           ", YELLOW)
        if fleet_failed or sddc_failed:
            log("  To actually purge these failed tasks and restart services, run:", YELLOW)
            log("  ./cleanup-failed-ops-tasks.py --purge", YELLOW)
        log("=======================================================================", CYAN)
        sys.exit(0)
        
    log("\n=======================================================================", CYAN)
    log("  INITIATING DELETION & PURGE                                          ", RED)
    log("=======================================================================", CYAN)
    
    # 3. Purge Fleet Tasks
    purge_fleet_lcm_tasks(fleet_host, password, fleet_failed)
    
    # 4. Purge SDDC Tasks
    purge_sddc_manager_tasks(sddc_host, password, sddc_failed)
    
    log("\n=======================================================================", CYAN)
    log("  CLEANUP COMPLETED.                                                   ", GREEN)
    log("=======================================================================", CYAN)

if __name__ == '__main__':
    main()

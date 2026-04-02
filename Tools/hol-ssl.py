#!/usr/bin/env python3
#
# hol-ssl.py - Issue SSL certificates for HOL vPods via HashiCorp Vault PKI
#
# version 2.2  2026-03-24
# Connects to a Vault PKI secrets engine to issue certificates. Vault connection
# details, PKI role, key parameters, and output directory are read from
# hol-ssl-config.yaml (or a user-supplied config file).
#
# Outputs: PEM cert, PEM key, CA cert, fullchain PEM, PKCS12/PFX, and JKS keystore.
#
# Usage examples:
#
#   ./hol-ssl.py -n my-webserver --ip 10.0.100.100
#   ./hol-ssl.py -n my-webserver.site-a.vcf.lab --ip 10.0.100.100
#   ./hol-ssl.py -n my-webserver --ip 10.0.100.100 --fqdn alias.vcf.lab
#   ./hol-ssl.py -c custom-config.yaml -n my-webserver --ip 10.0.100.100
#

import logging
import os
import sys
import socket
from argparse import ArgumentParser, RawDescriptionHelpFormatter

import yaml
import hvac
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12, BestAvailableEncryption
from cryptography.x509.oid import NameOID
import jks

VERSION = '2.2'
log = logging.getLogger('hol-ssl')

# ANSI colors (disabled when stdout is not a terminal)
if sys.stdout.isatty():
    _CYAN    = '\033[0;36m'
    _BLUE    = '\033[38;2;0;176;255m'
    _GREEN   = '\033[0;32m'
    _YELLOW  = '\033[1;33m'
    _BOLD    = '\033[1m'
    _NC      = '\033[0m'
else:
    _CYAN = _BLUE = _GREEN = _YELLOW = _BOLD = _NC = ''


# ---------------------------------------------------------------------------
# Vault interaction
# ---------------------------------------------------------------------------

def read_token(token_file):
    """Read a secret from a file (first line, stripped)."""
    try:
        with open(token_file, 'r') as fh:
            return fh.read().strip()
    except FileNotFoundError:
        sys.exit(f"ERROR: File not found: {token_file}")
    except PermissionError:
        sys.exit(f"ERROR: Permission denied reading: {token_file}")


def vault_client(url, token):
    """Return an authenticated hvac client after verifying connectivity."""
    client = hvac.Client(url=url, token=token)
    try:
        if not client.is_authenticated():
            sys.exit("ERROR: Vault authentication failed. Check token in the configured token_file.")
    except Exception as exc:
        sys.exit(f"ERROR: Cannot connect to Vault at {url}:\n  {exc}")
    return client


def ensure_role_max_ttl(client, mount, role, required_ttl_seconds):
    """
    Ensure the PKI role's max_ttl is at least `required_ttl_seconds`.
    Reads the full role config and writes it back with updated max_ttl
    to avoid losing existing role settings.
    """
    role_data = client.secrets.pki.read_role(role, mount_point=mount)
    raw_max = role_data['data'].get('max_ttl', 0)
    current_max = ttl_to_seconds(raw_max) if raw_max else 0
    if current_max < required_ttl_seconds:
        log.info("Updating Vault role '%s' max_ttl from %ds to %ds ...",
                 role, current_max, required_ttl_seconds)
        existing = role_data['data']
        existing['max_ttl'] = str(required_ttl_seconds)
        client.secrets.pki.create_or_update_role(
            role,
            mount_point=mount,
            extra_params=existing,
        )
        log.info("Role max_ttl updated successfully.")


def issue_certificate(client, mount, role, common_name, alt_names, ip_sans, ttl,
                      key_type, key_bits):
    """
    Issue a certificate via Vault's PKI issue endpoint.
    Returns the raw Vault response dict containing certificate, private_key,
    issuing_ca, ca_chain, and serial_number.
    """
    extra = {
        'ttl': ttl,
        'private_key_format': 'pem',
        'key_type': key_type,
        'key_bits': key_bits,
    }
    if alt_names:
        extra['alt_names'] = ','.join(alt_names)
    if ip_sans:
        extra['ip_sans'] = ','.join(ip_sans)

    try:
        resp = client.secrets.pki.generate_certificate(
            name=role,
            common_name=common_name,
            extra_params=extra,
            mount_point=mount,
        )
    except hvac.exceptions.VaultError as exc:
        sys.exit(f"ERROR: Vault certificate issuance failed:\n  {exc}")

    return resp['data']


# ---------------------------------------------------------------------------
# Crypto helpers — parse once, reuse everywhere
# ---------------------------------------------------------------------------

def parse_pem_objects(cert_pem, key_pem, ca_pem):
    """Parse PEM strings into cryptography objects (cert, key, CA cert)."""
    cert_obj = x509.load_pem_x509_certificate(cert_pem.encode())
    key_obj = serialization.load_pem_private_key(key_pem.encode(), password=None)
    ca_obj = x509.load_pem_x509_certificate(ca_pem.encode())
    return cert_obj, key_obj, ca_obj


# ---------------------------------------------------------------------------
# File output helpers
# ---------------------------------------------------------------------------

KEY_MATERIAL_PERMS = 0o600

def write_file(path, content, mode='w', permissions=None):
    """Write content to a file, optionally setting permissions. Returns path."""
    with open(path, mode) as fh:
        fh.write(content)
    if permissions is not None:
        os.chmod(path, permissions)
    return path


def build_pfx(cert_obj, key_obj, ca_obj, password, friendly_name):
    """Build a PKCS12/PFX blob from pre-parsed cryptography objects."""
    return pkcs12.serialize_key_and_certificates(
        name=friendly_name.encode(),
        key=key_obj,
        cert=cert_obj,
        cas=[ca_obj],
        encryption_algorithm=BestAvailableEncryption(password.encode()),
    )


def build_jks(cert_obj, key_obj, ca_obj, password, alias):
    """
    Build a Java KeyStore (JKS) containing a PrivateKeyEntry (key + cert chain)
    and a TrustedCertEntry for the CA, using pyjks.
    """
    cert_der = cert_obj.public_bytes(serialization.Encoding.DER)
    key_der = key_obj.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    ca_der = ca_obj.public_bytes(serialization.Encoding.DER)

    pke = jks.PrivateKeyEntry.new(alias=alias, certs=[cert_der, ca_der], key=key_der)
    ca_entry = jks.TrustedCertEntry.new('X.509', ca_der)

    keystore = jks.KeyStore.new('jks', [pke])
    keystore.entries['ca'] = ca_entry

    return keystore.saves(password)


# ---------------------------------------------------------------------------
# Certificate detail printer
# ---------------------------------------------------------------------------

def print_cert_details(cert_obj, vault_serial, role, ttl_requested):
    """Print human-readable certificate details from a pre-parsed cert object."""
    cn = cert_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    cn_str = cn[0].value if cn else '(none)'

    issuer_cn = cert_obj.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    issuer_str = issuer_cn[0].value if issuer_cn else '(none)'

    san_strs = []
    try:
        san_ext = cert_obj.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        for name in san_ext.value.get_values_for_type(x509.DNSName):
            san_strs.append(f"DNS:{name}")
        for name in san_ext.value.get_values_for_type(x509.IPAddress):
            san_strs.append(f"IP:{name}")
    except x509.ExtensionNotFound:
        pass

    not_before = cert_obj.not_valid_before_utc
    not_after = cert_obj.not_valid_after_utc

    log.info("")
    log.info("--- Certificate Details ---")
    log.info("  Common Name (CN):   %s", cn_str)
    log.info("  Issuer:             %s", issuer_str)
    log.info("  Serial Number:      %s", vault_serial)
    log.info("  Valid From:         %s", not_before.strftime('%Y-%m-%d %H:%M:%S UTC'))
    log.info("  Valid To:           %s", not_after.strftime('%Y-%m-%d %H:%M:%S UTC'))
    duration = not_after - not_before
    log.info("  Duration:           %d days", duration.days)
    log.info("  SANs:               %s", ', '.join(san_strs) if san_strs else '(none)')
    log.info("  Vault PKI Role:     %s", role)
    log.info("  TTL Requested:      %s", ttl_requested)


def print_file_summary(files):
    """Print a table of generated file paths and their sizes."""
    log.info("")
    log.info("--- Generated Files ---")
    log.info("  %-50s %10s", 'File', 'Size')
    log.info("  %s %s", '─' * 50, '─' * 10)
    for path in files:
        size = os.path.getsize(path)
        size_str = f"{size} B" if size < 1024 else f"{size / 1024:.1f} KB"
        log.info("  %-50s %10s", path, size_str)


# ---------------------------------------------------------------------------
# Collect SANs from config
# ---------------------------------------------------------------------------

def collect_sans(config_san, fqdn_override, ip_override, dom):
    """
    Build deduplicated lists of FQDNs and IP addresses from the SAN config,
    applying any command-line overrides for the primary FQDN and IP.
    The CN is returned separately; Vault adds it to the SAN automatically.
    """
    cn = config_san.get('commonName', f'localhost.{dom}')

    if fqdn_override:
        config_san['fqdn.1'] = fqdn_override
    elif not config_san.get('fqdn.1'):
        config_san['fqdn.1'] = cn

    if ip_override:
        config_san['ip_address.1'] = ip_override

    fqdns = [str(v) for k, v in sorted(config_san.items())
             if k.startswith('fqdn.') and v]
    ips = [str(v) for k, v in sorted(config_san.items())
           if k.startswith('ip_address.') and v]

    # Deduplicate preserving order; exclude CN (Vault adds it automatically)
    fqdns = list(dict.fromkeys(f for f in fqdns if f != cn))
    ips = list(dict.fromkeys(ips))

    return cn, fqdns, ips


# ---------------------------------------------------------------------------
# TTL helpers
# ---------------------------------------------------------------------------

# 398 days in seconds — maximum certificate lifetime that Firefox considers valid
# per Mozilla Root Store Policy (MRSP section 6.1).
TTL_398_DAYS_SECONDS = 398 * 24 * 60 * 60  # 34,387,200

def ttl_to_seconds(ttl_str):
    """
    Convert a Vault-style TTL string (e.g. '398d', '8760h', '34387200') to seconds.
    Supports 'd' (days), 'h' (hours), 'm' (minutes), 's' (seconds), or bare integer.
    """
    ttl_str = str(ttl_str).strip().lower()
    try:
        if ttl_str.endswith('d'):
            return int(ttl_str[:-1]) * 86400
        elif ttl_str.endswith('h'):
            return int(ttl_str[:-1]) * 3600
        elif ttl_str.endswith('m'):
            return int(ttl_str[:-1]) * 60
        elif ttl_str.endswith('s'):
            return int(ttl_str[:-1])
        else:
            return int(ttl_str)
    except ValueError:
        sys.exit(f"ERROR: Invalid TTL value: '{ttl_str}' — expected a number with optional "
                 "suffix (d/h/m/s), e.g. '398d', '8760h', '34387200'")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def detect_lab_domain():
    """
    Determine the HOL lab domain. Tries reverse-DNS on 10.0.0.2 first, then
    falls back to the first 'search' entry in /etc/resolv.conf, and finally
    defaults to 'site-a.vcf.lab'.
    """
    try:
        fqdn = socket.getfqdn('10.0.0.2')
        dot = fqdn.find('.')
        if dot > 0 and not fqdn[dot + 1:].replace('.', '').isdigit():
            return fqdn[dot + 1:]
    except OSError:
        pass

    try:
        with open('/etc/resolv.conf', 'r') as fh:
            for line in fh:
                parts = line.split()
                if parts and parts[0] == 'search' and len(parts) > 1:
                    candidates = parts[1:]
                    candidates.sort(key=len)
                    best = candidates[0]
                    for c in candidates[1:]:
                        if c.endswith('.' + best):
                            return best
                    if len(candidates) > 1:
                        first = candidates[0].split('.')
                        last = candidates[-1].split('.')
                        common = []
                        for a, b in zip(reversed(first), reversed(last)):
                            if a == b:
                                common.insert(0, a)
                            else:
                                break
                        if common:
                            return '.'.join(common)
                    return best
    except OSError:
        pass

    return 'site-a.vcf.lab'


def show_help(dom):
    """Print styled help text and exit."""
    W = 64
    title = 'HOL SSL Certificate Generator'
    ver = f'Version {VERSION}'
    print(f"{_CYAN}╔{'═' * W}╗{_NC}")
    print(f"{_CYAN}║{_NC}{_BLUE}{title:^{W}}{_NC}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{ver:^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}╚{'═' * W}╝{_NC}")
    print()
    print(f"{_BOLD}USAGE:{_NC}")
    print(f"    hol-ssl.py -n <hostname> [options]")
    print()
    print(f"{_BOLD}OPTIONS:{_NC}")
    print(f"    {_GREEN}-n, --hostname{_NC} <name>   {_BOLD}(required){_NC} Hostname or FQDN for the CN")
    print(f"                            Bare names get '.{dom}' appended automatically")
    print(f"    {_GREEN}-i, --ip{_NC} <address>      IPv4 address to include in the certificate SAN")
    print(f"    {_GREEN}-f, --fqdn{_NC} <fqdn>       Additional DNS SAN (default: hostname.{dom})")
    print(f"    {_GREEN}-c, --config{_NC} <file>      Path to YAML config (default: ./hol-ssl-config.yaml)")
    print(f"    {_GREEN}-v, --verbose{_NC}            Enable debug-level logging")
    print(f"    {_GREEN}-h, --help{_NC}               Show this help message")
    print()
    print(f"{_YELLOW}EXAMPLES:{_NC}")
    print(f"    {_GREEN}# Issue cert for a bare hostname (becomes my-webserver.{dom}){_NC}")
    print(f"    hol-ssl.py -n my-webserver --ip 10.0.100.100")
    print()
    print(f"    {_GREEN}# Issue cert with an explicit FQDN as the CN{_NC}")
    print(f"    hol-ssl.py -n my-webserver.site-a.{dom} --ip 10.0.100.100")
    print()
    print(f"    {_GREEN}# Add an extra DNS SAN alias{_NC}")
    print(f"    hol-ssl.py -n my-webserver --ip 10.0.100.100 --fqdn alias.{dom}")
    print()
    print(f"    {_GREEN}# Use a custom config file{_NC}")
    print(f"    hol-ssl.py -c /path/to/custom-config.yaml -n my-webserver --ip 10.0.100.100")
    print()
    print(f"{_CYAN}OUTPUT FILES:{_NC}")
    print(f"    <hostname>.crt           PEM certificate")
    print(f"    <hostname>.key           PEM private key (mode 0600)")
    print(f"    ca.crt                   CA certificate")
    print(f"    <hostname>-fullchain.crt Full chain (cert + CA)")
    print(f"    <hostname>.pfx           PKCS12 keystore")
    print(f"    <hostname>.jks           Java KeyStore (JKS)")
    print()
    print(f"{_CYAN}CONFIGURATION:{_NC}")
    print(f"    Default config:  ./hol-ssl-config.yaml")
    print(f"    Vault, SAN entries, key type, TTL, and output directory are")
    print(f"    all controlled via the YAML config file. See the config file")
    print(f"    comments for details.")
    print()
    sys.exit(0)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    dom = detect_lab_domain()

    if len(sys.argv) == 1 or '--help' in sys.argv or '-h' in sys.argv:
        show_help(dom)

    class _HelpOnErrorParser(ArgumentParser):
        """Show the custom help screen on parse errors instead of argparse's default."""
        def error(self, message):
            sys.stderr.write(f"ERROR: {message}\n\n")
            show_help(dom)

    parser = _HelpOnErrorParser(add_help=False)
    parser.add_argument("-n", "--hostname", required=True, dest="host_name")
    parser.add_argument("-c", "--config", required=False, dest="config_file",
                        default='./hol-ssl-config.yaml')
    parser.add_argument("-f", "--fqdn", required=False, dest="fqdn")
    parser.add_argument("-i", "--ip", required=False, dest="ip_address")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ---- Load configuration ------------------------------------------------
    try:
        with open(args.config_file, 'r') as fh:
            cfg = yaml.safe_load(fh)
    except FileNotFoundError:
        sys.exit(f"ERROR: Config file not found: {args.config_file}")
    except yaml.YAMLError as exc:
        sys.exit(f"ERROR: Invalid YAML in {args.config_file}:\n  {exc}")

    for section in ('VAULT', 'SAN', 'CERT'):
        if section not in cfg:
            sys.exit(f"ERROR: Missing required section '{section}' in {args.config_file}")

    vault_cfg = cfg['VAULT']
    san_cfg = cfg['SAN']
    cert_cfg = cfg['CERT']

    host_name = args.host_name.strip()
    if ' ' in host_name:
        sys.exit(f"ERROR: Hostname cannot contain spaces: '{args.host_name}'")

    # Vault PKI role requires the CN to be under an allowed domain (e.g. vcf.lab).
    # If the user supplies a bare hostname, append the lab domain automatically.
    if '.' in host_name:
        cn = host_name
    else:
        cn = f"{host_name}.{dom}"
    san_cfg['commonName'] = cn

    ttl = cert_cfg.get('ttl', '398d')
    ttl_seconds = ttl_to_seconds(ttl)
    key_type = cert_cfg.get('key_type', 'rsa')
    key_bits = int(cert_cfg.get('key_bits', 2048))
    cert_dir = cert_cfg.get('cert_dir', '/hol/ssl')

    pfx_pw_file = cert_cfg.get('pfx_password_file')
    if not pfx_pw_file:
        log.warning("No 'pfx_password_file' in CERT config — falling back to Vault token_file. "
                     "Consider setting a dedicated password file.")
        pfx_pw_file = vault_cfg['token_file']

    cn, alt_names, ip_sans = collect_sans(san_cfg, args.fqdn, args.ip_address, dom)

    # ---- Read secrets from files -------------------------------------------
    vault_token = read_token(vault_cfg['token_file'])
    pfx_password = read_token(pfx_pw_file)

    mount = vault_cfg.get('pki_mount', 'pki')
    role = vault_cfg.get('pki_role', 'holodeck')

    # ---- Connect to Vault --------------------------------------------------
    log.info("Connecting to Vault at %s ...", vault_cfg['url'])
    client = vault_client(vault_cfg['url'], vault_token)
    log.info("  Authenticated successfully.")

    # ---- Ensure role max_ttl supports the requested TTL --------------------
    log.info("\nChecking Vault PKI role '%s' max_ttl ...", role)
    ensure_role_max_ttl(client, mount, role, ttl_seconds)

    # ---- Issue the certificate ---------------------------------------------
    log.info("\nIssuing certificate for CN=%s via role '%s' ...", cn, role)
    log.info("  SANs (DNS): %s", ', '.join(alt_names) if alt_names else '(none)')
    log.info("  SANs (IP):  %s", ', '.join(ip_sans) if ip_sans else '(none)')
    log.info("  TTL:        %s  (%ds / %d days)", ttl, ttl_seconds, ttl_seconds // 86400)
    log.info("  Key:        %s %d-bit", key_type.upper(), key_bits)

    cert_data = issue_certificate(
        client, mount, role, cn,
        alt_names=alt_names,
        ip_sans=ip_sans,
        ttl=ttl,
        key_type=key_type,
        key_bits=key_bits,
    )

    cert_pem = cert_data['certificate']
    key_pem = cert_data['private_key']
    ca_pem = cert_data['issuing_ca']
    serial = cert_data['serial_number']
    fullchain_pem = cert_pem + '\n' + ca_pem

    # Parse PEM objects once for all downstream consumers
    cert_obj, key_obj, ca_obj = parse_pem_objects(cert_pem, key_pem, ca_pem)

    # ---- Write output files ------------------------------------------------
    os.makedirs(cert_dir, exist_ok=True)
    generated_files = []

    cert_path = os.path.join(cert_dir, f'{host_name}.crt')
    key_path = os.path.join(cert_dir, f'{host_name}.key')
    ca_path = os.path.join(cert_dir, 'ca.crt')
    chain_path = os.path.join(cert_dir, f'{host_name}-fullchain.crt')
    pfx_path = os.path.join(cert_dir, f'{host_name}.pfx')
    jks_path = os.path.join(cert_dir, f'{host_name}.jks')

    # PEM certificate
    write_file(cert_path, cert_pem + '\n')
    generated_files.append(cert_path)

    # PEM private key
    write_file(key_path, key_pem + '\n', permissions=KEY_MATERIAL_PERMS)
    generated_files.append(key_path)

    # CA certificate
    write_file(ca_path, ca_pem + '\n')
    generated_files.append(ca_path)

    # Full chain (cert + CA)
    write_file(chain_path, fullchain_pem + '\n')
    generated_files.append(chain_path)

    # PKCS12/PFX
    pfx_bytes = build_pfx(cert_obj, key_obj, ca_obj, pfx_password, host_name)
    write_file(pfx_path, pfx_bytes, mode='wb', permissions=KEY_MATERIAL_PERMS)
    generated_files.append(pfx_path)

    # Java KeyStore (JKS)
    jks_bytes = build_jks(cert_obj, key_obj, ca_obj, pfx_password, host_name)
    write_file(jks_path, jks_bytes, mode='wb', permissions=KEY_MATERIAL_PERMS)
    generated_files.append(jks_path)

    # ---- Print summary -----------------------------------------------------
    print_cert_details(cert_obj, serial, role, ttl)
    print_file_summary(generated_files)
    log.info("\n=== Finished ===")

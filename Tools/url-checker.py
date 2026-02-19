#!/usr/bin/env python3
# url-checker.py - HOLFY27 Standalone URL Checker
# Version 1.0 - February 2026
# Author - Burke Azbill and HOL Core Team
# Standalone URL checker that allows testing URLs similar to labstartup
# without running the full labstartup process or adding config entries.

import sys
import argparse
import time
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SCRIPT_VERSION = '1.0'


def log(msg):
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{timestamp}] {msg}')


def check_url(url, expected_text=None, max_retries=1, retry_delay=5, timeout=15):
    session = requests.Session()
    session.trust_env = False

    last_error = None

    for attempt in range(1, max_retries + 1):
        log(f'Attempt {attempt}/{max_retries} - GET {url}')
        try:
            response = session.get(
                url,
                verify=False,
                timeout=timeout,
                proxies=None,
                allow_redirects=True
            )

            log(f'  HTTP status: {response.status_code}')

            if response.status_code != 200:
                last_error = f'HTTP {response.status_code}'
                if attempt < max_retries:
                    log(f'  Non-200 status, retrying in {retry_delay}s...')
                    time.sleep(retry_delay)
                continue

            if expected_text and expected_text not in response.text:
                last_error = f'Expected text "{expected_text}" not found in response'
                log(f'  {last_error}')
                if attempt < max_retries:
                    log(f'  Retrying in {retry_delay}s...')
                    time.sleep(retry_delay)
                continue

            if expected_text:
                log(f'  Expected text "{expected_text}" found')
            log(f'  SUCCESS on attempt {attempt}')
            return True, attempt, None

        except requests.exceptions.SSLError as e:
            last_error = f'SSL error: {e}'
        except requests.exceptions.ConnectionError as e:
            last_error = f'Connection error: {e}'
        except requests.exceptions.Timeout:
            last_error = 'Request timeout'
        except Exception as e:
            last_error = f'Unexpected error: {e}'

        log(f'  {last_error}')
        if attempt < max_retries:
            log(f'  Retrying in {retry_delay}s...')
            time.sleep(retry_delay)

    log(f'FAILED after {max_retries} attempt(s)')
    return False, max_retries, last_error


def main():
    parser = argparse.ArgumentParser(
        description='Standalone URL checker for testing URLs without running '
                    'the full labstartup process or adding config entries.'
    )
    parser.add_argument('url', help='URL to check')
    parser.add_argument('-e', '--expected-text', default=None,
                        help='Text expected in the response body')
    parser.add_argument('-r', '--max-retries', type=int, default=1,
                        help='Maximum number of retry attempts (default: 1)')
    parser.add_argument('-d', '--retry-delay', type=int, default=5,
                        help='Seconds to wait between retries (default: 5)')
    parser.add_argument('-t', '--timeout', type=int, default=15,
                        help='Request timeout in seconds (default: 15)')
    parser.add_argument('-v', '--version', action='version',
                        version=f'url-checker.py {SCRIPT_VERSION}')

    args = parser.parse_args()

    log(f'url-checker.py v{SCRIPT_VERSION}')
    log(f'URL:          {args.url}')
    log(f'Expected text: {args.expected_text or "(none)"}')
    log(f'Max retries:  {args.max_retries}')
    log(f'Retry delay:  {args.retry_delay}s')
    log(f'Timeout:      {args.timeout}s')
    log('')

    success, attempts, error = check_url(
        args.url,
        expected_text=args.expected_text,
        max_retries=args.max_retries,
        retry_delay=args.retry_delay,
        timeout=args.timeout,
    )

    log('')
    if success:
        log(f'RESULT: SUCCESS ({attempts} attempt(s))')
        sys.exit(0)
    else:
        log(f'RESULT: FAILED ({attempts} attempt(s)) - {error}')
        sys.exit(1)


if __name__ == '__main__':
    main()

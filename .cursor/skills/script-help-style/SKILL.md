---
name: script-help-style
description: >-
  Enforce a consistent, colored, user-friendly help screen style for CLI
  scripts in Python and Bash. Covers banner, sections, ANSI colors, error
  handling, and terminal-safe output. Use when creating a new script, adding
  --help output, building a CLI tool, or writing argument parsing code.
---

# Script Help Style Guide

All CLI scripts must display a styled help screen following this standard.
The style matches existing tools like `hol-ssl.py` and `tdns-mgr`.

## Layout — Required Sections (in order)

1. **Banner** — Colored box with tool name + version, centered text
2. **USAGE** — One-line synopsis: `script-name <required> [optional]`
3. **OPTIONS** — Flag table with short/long forms, value placeholders, descriptions
4. **EXAMPLES** — 2-4 real-world invocations, each preceded by a green comment
5. **Additional sections** (optional) — OUTPUT FILES, CONFIGURATION, ENVIRONMENT VARIABLES, etc.

## Color Palette

Use these ANSI codes consistently. Always gate on terminal detection.

| Color    | ANSI Code                | Used For                              |
|----------|--------------------------|---------------------------------------|
| CYAN     | `\033[0;36m`             | Banner box borders, info section headers |
| BLUE     | `\033[38;2;0;176;255m`   | Tool name in banner                   |
| GREEN    | `\033[0;32m`             | Flag names, example comments          |
| YELLOW   | `\033[1;33m`             | EXAMPLES section header               |
| BOLD     | `\033[1m`                | USAGE/OPTIONS headers, `(required)` tag |
| NC       | `\033[0m`                | Reset                                 |

## Python Implementation

### Color setup (terminal-safe)

```python
import sys

if sys.stdout.isatty():
    _CYAN    = '\033[0;36m'
    _BLUE    = '\033[38;2;0;176;255m'
    _GREEN   = '\033[0;32m'
    _YELLOW  = '\033[1;33m'
    _BOLD    = '\033[1m'
    _NC      = '\033[0m'
else:
    _CYAN = _BLUE = _GREEN = _YELLOW = _BOLD = _NC = ''
```

### Banner

```python
def show_help():
    W = 64
    title = 'My Tool Name'
    ver = f'Version {VERSION}'
    print(f"{_CYAN}╔{'═' * W}╗{_NC}")
    print(f"{_CYAN}║{_NC}{_BLUE}{title:^{W}}{_NC}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{ver:^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}╚{'═' * W}╝{_NC}")
```

### Options formatting

```python
    print(f"{_BOLD}OPTIONS:{_NC}")
    print(f"    {_GREEN}-n, --name{_NC} <value>      {_BOLD}(required){_NC} Description here")
    print(f"    {_GREEN}-o, --optional{_NC} <val>     Optional flag description")
    print(f"    {_GREEN}-v, --verbose{_NC}            Enable debug logging")
    print(f"    {_GREEN}-h, --help{_NC}               Show this help message")
```

Rules for the options block:
- Short flag first, then long: `-n, --name`
- Flags in GREEN, value placeholder in default color
- Required flags marked with BOLD `(required)` tag
- Descriptions aligned to a consistent column (column ~30)
- Continuation lines indented to match the description column

### Examples formatting

```python
    print(f"{_YELLOW}EXAMPLES:{_NC}")
    print(f"    {_GREEN}# Simple usage{_NC}")
    print(f"    my-tool.py -n my-value")
    print()
    print(f"    {_GREEN}# With all options{_NC}")
    print(f"    my-tool.py -n my-value -o extra --verbose")
```

### Argument parser integration

Override argparse so errors show the custom help instead of the default:

```python
from argparse import ArgumentParser

def show_help():
    # ... styled help ...
    sys.exit(0)

if len(sys.argv) == 1 or '--help' in sys.argv or '-h' in sys.argv:
    show_help()

class _HelpOnErrorParser(ArgumentParser):
    def error(self, message):
        sys.stderr.write(f"ERROR: {message}\n\n")
        show_help()

parser = _HelpOnErrorParser(add_help=False)
# add arguments with add_help=False, no help= strings needed
```

## Bash Implementation

### Color setup (terminal-safe)

```bash
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[38;2;0;176;255m'
    CYAN='\033[0;36m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED='' GREEN='' YELLOW='' BLUE='' CYAN='' BOLD='' NC=''
fi
```

### Banner

```bash
print_header() {
    echo -e "${CYAN}╔════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║${NC}${BLUE}$(printf '%*s' $(( (64 + ${#TOOL_NAME}) / 2 )) "$TOOL_NAME")$(printf '%*s' $(( (64 - ${#TOOL_NAME}) / 2 )) '')${NC}${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}$(printf '%*s' $(( (64 + ${#VERSION_STR}) / 2 )) "$VERSION_STR")$(printf '%*s' $(( (64 - ${#VERSION_STR}) / 2 )) '')${NC}${CYAN}║${NC}"
    echo -e "${CYAN}╚════════════════════════════════════════════════════════════════╝${NC}"
}
```

### Help function

```bash
show_help() {
    print_header
    echo ""
    echo -e "${BOLD}USAGE:${NC}"
    echo -e "    my-tool.sh [options] <command>"
    echo ""
    echo -e "${BOLD}OPTIONS:${NC}"
    echo -e "    ${GREEN}-n, --name${NC} <value>      ${BOLD}(required)${NC} Description"
    echo -e "    ${GREEN}-v, --verbose${NC}            Enable debug output"
    echo -e "    ${GREEN}-h, --help${NC}               Show this help message"
    echo ""
    echo -e "${YELLOW}EXAMPLES:${NC}"
    echo -e "    ${GREEN}# Simple usage${NC}"
    echo -e "    my-tool.sh -n my-value"
    echo ""
    echo -e "    ${GREEN}# With options${NC}"
    echo -e "    my-tool.sh -n my-value --verbose"
    echo ""
}
```

## Rules

1. **No args = help.** Running the script with no arguments must show the help screen, not an error traceback.
2. **Exit 0 on help.** Help display exits cleanly with code 0.
3. **Errors show help.** Parse errors print `ERROR: <message>` to stderr, then show help, then exit.
4. **Terminal-safe.** Colors are disabled when stdout is not a TTY (piping, redirection).
5. **Banner width = 64.** Inner width is always 64 characters; title and version are centered.
6. **Box-drawing characters.** Use Unicode: `╔ ═ ╗ ║ ╚ ╝` — never ASCII `+--+`.
7. **4-space indent.** All content inside sections is indented 4 spaces.
8. **Blank line between examples.** Each example is separated by a blank line.
9. **VERSION constant.** Version is defined once as a constant, referenced in both the banner and the file header comment.

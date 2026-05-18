---
name: script-help-style
description: "Enforce a consistent, colored, user-friendly help screen style for CLI scripts in Python and Bash. Covers banner, sections, ANSI colors, error handling, and terminal-safe output. Use when creating a new script, adding --help output, building a CLI tool, or writing argument parsing code."
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

## Quick Start

1. Copy the color setup block (gate on `sys.stdout.isatty()` / `[[ -t 1 ]]`)
2. Define a `VERSION` constant at the top of your script
3. Implement `show_help()` following the layout order above
4. Wire up: no args → help, `--help`/`-h` → help, parse error → stderr message + help

### Minimal Python Example

```python
import sys

if sys.stdout.isatty():
    _CYAN, _BLUE, _GREEN, _BOLD, _YELLOW, _NC = (
        '\033[0;36m', '\033[38;2;0;176;255m', '\033[0;32m', '\033[1m', '\033[1;33m', '\033[0m')
else:
    _CYAN = _BLUE = _GREEN = _BOLD = _YELLOW = _NC = ''

VERSION = '1.0.0'

def show_help():
    W = 64
    title = 'My Tool'
    print(f"{_CYAN}╔{'═' * W}╗{_NC}")
    print(f"{_CYAN}║{_NC}{_BLUE}{title:^{W}}{_NC}{_CYAN}║{_NC}")
    print(f"{_CYAN}║{_NC}{f'Version {VERSION}':^{W}}{_CYAN}║{_NC}")
    print(f"{_CYAN}╚{'═' * W}╝{_NC}\n")
    print(f"{_BOLD}USAGE:{_NC}\n    my-tool.py <name> [--verbose]\n")
    print(f"{_BOLD}OPTIONS:{_NC}")
    print(f"    {_GREEN}-n, --name{_NC} <value>      {_BOLD}(required){_NC} Target name")
    print(f"    {_GREEN}-h, --help{_NC}               Show this help message\n")
    print(f"{_YELLOW}EXAMPLES:{_NC}")
    print(f"    {_GREEN}# Basic usage{_NC}")
    print(f"    my-tool.py -n my-value")
    sys.exit(0)

if len(sys.argv) == 1 or '--help' in sys.argv or '-h' in sys.argv:
    show_help()
```

## Full Implementations

Complete copy-paste-ready code with argparse integration, error handling, and all formatting details:

- **Python**: See [references/python-impl.md](references/python-impl.md) — includes `_HelpOnErrorParser` argparse override
- **Bash**: See [references/bash-impl.md](references/bash-impl.md) — includes `print_header` and `show_help` functions

### Options Block Rules

- Short flag first, then long: `-n, --name`
- Flags in GREEN, value placeholder in default color
- Required flags marked with BOLD `(required)` tag
- Descriptions aligned to a consistent column (column ~30)
- Continuation lines indented to match the description column

## Rules

1. **No args = help.** Running the script with no arguments must show the help screen, not an error traceback.
2. **Errors show help.** Parse errors print `ERROR: <message>` to stderr, then show help, then exit 1.
3. **Box-drawing characters.** Use Unicode: `╔ ═ ╗ ║ ╚ ╝` — never ASCII `+--+`.
4. **VERSION constant.** Version is defined once as a constant, referenced in both the banner and the file header comment.

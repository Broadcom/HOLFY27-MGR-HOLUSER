# Python CLI Help Implementation

## Color Setup (terminal-safe)

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

## Banner

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

## Options Formatting

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

## Examples Formatting

```python
    print(f"{_YELLOW}EXAMPLES:{_NC}")
    print(f"    {_GREEN}# Simple usage{_NC}")
    print(f"    my-tool.py -n my-value")
    print()
    print(f"    {_GREEN}# With all options{_NC}")
    print(f"    my-tool.py -n my-value -o extra --verbose")
```

## Argument Parser Integration

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

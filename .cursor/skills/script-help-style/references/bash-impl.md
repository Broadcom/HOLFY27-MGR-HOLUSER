# Bash CLI Help Implementation

## Color Setup (terminal-safe)

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

## Banner

```bash
print_header() {
    echo -e "${CYAN}╔════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║${NC}${BLUE}$(printf '%*s' $(( (64 + ${#TOOL_NAME}) / 2 )) "$TOOL_NAME")$(printf '%*s' $(( (64 - ${#TOOL_NAME}) / 2 )) '')${NC}${CYAN}║${NC}"
    echo -e "${CYAN}║${NC}$(printf '%*s' $(( (64 + ${#VERSION_STR}) / 2 )) "$VERSION_STR")$(printf '%*s' $(( (64 - ${#VERSION_STR}) / 2 )) '')${NC}${CYAN}║${NC}"
    echo -e "${CYAN}╚════════════════════════════════════════════════════════════════╝${NC}"
}
```

## Help Function

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

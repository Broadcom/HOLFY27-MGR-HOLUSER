#!/bin/sh
# nofirewall.sh - HOLFY27 No Firewall Configuration
# Version 1.0 - January 2026
# Author - Burke Azbill and HOL Core Team
# This file is pushed to router for non-HOL lab types
# It sets a permissive firewall policy

echo "Configuring permissive firewall..."

# Flush existing rules
iptables --flush
ip6tables --flush

# Set default policies to ACCEPT
iptables -P INPUT ACCEPT
iptables -P OUTPUT ACCEPT
iptables -P FORWARD ACCEPT

# Keep NAT for routing
iptables -t nat -A POSTROUTING -o eth1 -j MASQUERADE
iptables -t nat -A POSTROUTING -o eth2 -j MASQUERADE

# Create indicator file
true > /home/holuser/firewall

echo "Permissive firewall configured at $(date)"

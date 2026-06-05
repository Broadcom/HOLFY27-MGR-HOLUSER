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

# allow ssh on the Manager
iptables -A FORWARD -p tcp -d 10.1.10.131 --dport 22 -j ACCEPT
iptables -A FORWARD -p tcp -s 10.1.10.131 --sport 22 -j ACCEPT

# DNAT/SNAT ssh on the Manager over external port 5480
iptables -A PREROUTING -t nat -d 192.168.0.2 -p tcp --dport 5480 -j DNAT --to-d 10.1.10.131:22
iptables -A POSTROUTING -t nat -p tcp -d 10.1.10.131 --dport 22 -j SNAT --to-source 192.168.0.2:5480

# for VLP Agent open access on the Manager VM 
iptables -A FORWARD -p tcp -s 10.1.10.131 -d 0.0.0.0/0 -j ACCEPT
iptables -A FORWARD -p tcp -d 10.1.10.131 -j ACCEPT

# allow ssh on the Main Console
iptables -A FORWARD -p tcp -d 10.1.10.130 --dport 22 -j ACCEPT
iptables -A FORWARD -p tcp -s 10.1.10.130  --sport 22 -j ACCEPT

# DNAT/SNAT port forward ssh to the Main Console
iptables -A PREROUTING -t nat -p tcp -d 192.168.0.2 --dport 22 -j DNAT --to 10.1.10.130:22
iptables -A POSTROUTING -t nat -p tcp -d 10.1.10.130 --dport 22 -j SNAT --to-source 192.168.0.2

# allow 5901 on the Main Console
iptables -A FORWARD -p tcp -d 10.1.10.130 --dport 5901 -j ACCEPT
iptables -A FORWARD -p tcp -s 10.1.10.130 --sport 5901 -j ACCEPT

# DNAT/SNAT port forward screen sharing over 5901 to the Main Console
iptables -A PREROUTING -t nat -p tcp -d 192.168.0.2 --dport 5901 -j DNAT --to 10.1.10.130:5901
iptables -A POSTROUTING -t nat -p tcp -d 10.1.10.130 --dport 5901 -j SNAT --to-source 192.168.0.2

# allow RDP 3389 on the Main Console
iptables -A FORWARD -p tcp -d 10.1.10.130 --dport 3389 -j ACCEPT
iptables -A FORWARD -p tcp -s 10.1.10.130 --sport 3389 -j ACCEPT

# DNAT/SNAT port forward 3389 for RDP to the Main Console
iptables -A PREROUTING -t nat -p tcp -d 192.168.0.2 --dport 3389 -j DNAT --to 10.1.10.130:3389
iptables -A POSTROUTING -t nat -p tcp -d 10.1.10.130 --dport 3389 -j SNAT --to-source 192.168.0.2

iptables -A INPUT -p tcp --sport 3128 -m state --state NEW,ESTABLISHED -j ACCEPT
iptables -A OUTPUT -p tcp --dport 3128 -m state --state ESTABLISHED -j ACCEPT

# Create indicator file
true > /home/holuser/firewall

echo "Permissive firewall configured at [$(date '+%Y-%m-%d %H:%M:%S')]"

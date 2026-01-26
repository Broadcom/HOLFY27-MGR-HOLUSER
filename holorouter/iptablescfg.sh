#!/bin/sh

# DEFAULT HOL FIREWALL RULESET
# version 08-April 2025

# clear any existing rules
iptables --flush
ip6tables --flush

# establish holodeck standard rules
iptables -P INPUT ACCEPT
iptables -P OUTPUT ACCEPT
# chains already exist no need to try to create again
#iptables -N FLANNEL-FWD
#iptables -N KUBE-EXTERNAL-SERVICES
#iptables -N KUBE-FIREWALL
#iptables -N KUBE-FORWARD
#iptables -N KUBE-KUBELET-CANARY
#iptables -N KUBE-NODEPORTS
#iptables -N KUBE-PROXY-CANARY
#iptables -N KUBE-PROXY-FIREWALL
#iptables -N KUBE-SERVICES
iptables -A INPUT -m conntrack --ctstate NEW -m comment --comment "kubernetes load balancer firewall" -j KUBE-PROXY-FIREWALL
iptables -A INPUT -m comment --comment "kubernetes health check service ports" -j KUBE-NODEPORTS
iptables -A INPUT -m conntrack --ctstate NEW -m comment --comment "kubernetes externally-visible service portals" -j KUBE-EXTERNAL-SERVICES
iptables -A INPUT -j KUBE-FIREWALL
iptables -A INPUT -p tcp -m tcp -m multiport --dports 111,2049,20048 -j ACCEPT
iptables -A INPUT -p udp -m udp --dport 123 -j ACCEPT
iptables -A INPUT -p udp -m udp --dport 53 -j ACCEPT
iptables -A INPUT -p udp -m udp --dport 67 -j ACCEPT
iptables -A INPUT -p udp -m udp --dport 68 -j ACCEPT
iptables -A INPUT -p tcp -m tcp --dport 53 -j ACCEPT
iptables -A INPUT -p tcp -m tcp --dport 67 -j ACCEPT
iptables -A INPUT -p tcp -m tcp --dport 68 -j ACCEPT
iptables -A INPUT -p tcp -m tcp --dport 179 -j ACCEPT
iptables -A INPUT -p tcp -m tcp --dport 2379:2380 -j ACCEPT
iptables -A INPUT -p tcp -m tcp --dport 4789 -j ACCEPT
iptables -A INPUT -p tcp -m tcp --dport 6443 -j ACCEPT
iptables -A INPUT -p tcp -m tcp --dport 3128 -j ACCEPT
iptables -A INPUT -p tcp -m tcp --dport 10250:10252 -j ACCEPT
iptables -A INPUT -p icmp -m icmp --icmp-type 8 -j ACCEPT
iptables -A FORWARD -m conntrack --ctstate NEW -m comment --comment "kubernetes load balancer firewall" -j KUBE-PROXY-FIREWALL
iptables -A FORWARD -m comment --comment "kubernetes forwarding rules" -j KUBE-FORWARD
iptables -A FORWARD -m conntrack --ctstate NEW -m comment --comment "kubernetes service portals" -j KUBE-SERVICES
iptables -A FORWARD -m conntrack --ctstate NEW -m comment --comment "kubernetes externally-visible service portals" -j KUBE-EXTERNAL-SERVICES
iptables -A FORWARD -m comment --comment "flanneld forward" -j FLANNEL-FWD
iptables -A OUTPUT -m conntrack --ctstate NEW -m comment --comment "kubernetes load balancer firewall" -j KUBE-PROXY-FIREWALL
iptables -A OUTPUT -m conntrack --ctstate NEW -m comment --comment "kubernetes service portals" -j KUBE-SERVICES
iptables -A OUTPUT -j KUBE-FIREWALL
iptables -A OUTPUT -p tcp -m tcp -m multiport --dports 111,2049,20048 -j ACCEPT
iptables -A FLANNEL-FWD -s 10.244.0.0/16 -m comment --comment "flanneld forward" -j ACCEPT
iptables -A FLANNEL-FWD -d 10.244.0.0/16 -m comment --comment "flanneld forward" -j ACCEPT
iptables -A KUBE-FIREWALL ! -s 127.0.0.0/8 -d 127.0.0.0/8 -m comment --comment "block incoming localnet connections" -m conntrack ! --ctstate RELATED,ESTABLISHED,DNAT -j DROP
iptables -A KUBE-FORWARD -m conntrack --ctstate INVALID -j DROP
iptables -A KUBE-FORWARD -m comment --comment "kubernetes forwarding rules" -m mark --mark 0x4000/0x4000 -j ACCEPT
iptables -A KUBE-FORWARD -m comment --comment "kubernetes forwarding conntrack rule" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

iptables -t nat -A POSTROUTING -o eth1 -j MASQUERADE
iptables -t nat -A POSTROUTING -o eth2 -j MASQUERADE

# EXAMPLE allow SSH: do not use as-is. Too open!
#iptables -A FORWARD -s 192.168.110.0/24 -p TCP --dport 22 -j ACCEPT

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

# allow access to and from Google DNS
iptables -A FORWARD -p UDP -d 8.8.8.8 --dport 53 -j ACCEPT
iptables -A FORWARD -p UDP -s 8.8.8.8 --sport 53 -j ACCEPT
iptables -A FORWARD -p UDP -d 8.8.4.4 --dport 53 -j ACCEPT
iptables -A FORWARD -p UDP -s 8.8.4.4 --sport 53 -j ACCEPT

# allow ping everywhere
iptables -A FORWARD -p icmp --icmp-type 8 -s 0/0 -d 0/0 -m state --state NEW,ESTABLISHED,RELATED -j ACCEPT
iptables -A FORWARD -p icmp --icmp-type 0 -s 0/0 -d 0/0 -m state --state ESTABLISHED,RELATED -j ACCEPT

# allow IPs inside the vPod, only on private networks TBD
iptables -A FORWARD -s 192.168.0.0/16 -d 192.168.0.0/16 -j ACCEPT
iptables -A FORWARD -s 192.168.0.0/16 -d 172.16.0.0/12  -j ACCEPT
iptables -A FORWARD -s 192.168.0.0/16 -d 10.0.0.0/8     -j ACCEPT
iptables -A FORWARD -s 172.16.0.0/12 -d 192.168.0.0/16  -j ACCEPT
iptables -A FORWARD -s 172.16.0.0/12 -d 172.16.0.0/12   -j ACCEPT
iptables -A FORWARD -s 172.16.0.0/12 -d 10.0.0.0/8      -j ACCEPT
iptables -A FORWARD -s 10.0.0.0/8 -d 192.168.0.0/16     -j ACCEPT
iptables -A FORWARD -s 10.0.0.0/8 -d 172.16.0.0/12      -j ACCEPT
iptables -A FORWARD -s 10.0.0.0/8 -d 10.0.0.0/8         -j ACCEPT
# Allow access to Broadcom package repository
#This new rule automatically resolves the domain name projects.packages.broadcom.com to its current IP addresses and adds a corresponding iptables ACCEPT rule for each IP address in the FORWARD chain.
# for ip in $(dig +short projects.packages.broadcom.com | grep -oE "\b([0-9]{1,3}\.){3}[0-9]{1,3}\b")
# do
#   iptables -A FORWARD -d $ip -j ACCEPT
# done

### LAB-SPECIFIC RULES
ip6tables -P FORWARD ACCEPT
ip6tables -P INPUT ACCEPT
ip6tables -P OUTPUT ACCEPT
# (add your rules here)

### END RULES

#set the default policy on FORWARD to DROP (deny all)
iptables -P FORWARD DROP

# indicate that iptables has run
echo "" > ~holuser/firewall

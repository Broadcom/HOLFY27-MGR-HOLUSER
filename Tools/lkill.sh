#!/usr/bin/sh

# kill parent labstartup.py process
lproc=`ps -ef | grep python | grep labstartup.py | awk '{print $2}'`
if [ "${lproc}" ];then
   echo "Killing parent labstartup.py process: $lproc"
   kill $lproc
else
   echo "LabStartup is not running."
   exit 1
fi

# then kill child process
cproc=`ps -ef | grep python | grep Startup | awk '{print $2}'`
if [ "${cproc}" ];then
   echo "Killing labstartup child process: $cproc"
   kill $cproc
else
   echo "Child LabStartup process is not running."
   exit 1
fi
# checkfw.py version 1.3 22-February 2025
import socket
import platform

global os_type
os_type = platform.system()

def test_tcp_port(server, port):
    """
    attempt a socket connection to the host on the port
    :param server:
    :param port:
    :return: boolean, true it connection is sucessful
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    if os_type == 'Linux':
        logfile = '/tmp/checkfw.err'
    else:
        logfile = 'C:\\TEMP\\checkfw.err'
    try:
        s.connect((server, int(port)))
        s.shutdown(2)
        print(f'Successfully connected to {server} on port {port}')
        return True
    except IOError as e:
        print(e)
        with open(logfile, "a") as lf:
            lf.write(f'{e}\n')
            lf.close()
        return False


status = test_tcp_port('www.broadcom.com', 443)
if status == False:
    print("Good")
else:
    print("Bad")

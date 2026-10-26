"""Step 1 helper: look for any local video stream on a camera.

Checks the usual TCP ports, asks HTTP ports what they are, sends an RTSP
OPTIONS request to RTSP ports, and sends an ONVIF WS-Discovery probe (UDP 3702).
Only talks to the single IP you give it.
"""

import socket
import uuid

TCP_PORTS = {
    80: "HTTP", 81: "HTTP alt", 443: "HTTPS", 554: "RTSP", 8000: "ONVIF/HTTP alt",
    8080: "HTTP alt", 8554: "RTSP alt", 8899: "ONVIF alt", 6123: "A9/V720 control (AP mode)",
    23: "telnet", 1883: "MQTT",
}

WS_DISCOVERY = f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<e:Header><w:MessageID>uuid:{uuid.uuid4()}</w:MessageID>
<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>"""


def _talk(ip, port, payload: bytes, timeout=3.0) -> str:
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.sendall(payload)
            return s.recv(512).decode("latin-1", "replace").split("\r\n\r\n")[0]
    except OSError as e:
        return f"(no answer: {e})"


def probe(ip: str, timeout: float = 1.5):
    print(f"Probing {ip} ...")
    open_ports = []
    for port, what in TCP_PORTS.items():
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                open_ports.append(port)
                print(f"  TCP {port:5d} OPEN    ({what})")
        except OSError:
            print(f"  TCP {port:5d} closed  ({what})")

    for port in open_ports:
        if TCP_PORTS[port].startswith("HTTP") or port == 8000:
            print(f"\n  HTTP {port} says:\n    " +
                  _talk(ip, port, f"GET / HTTP/1.0\r\nHost: {ip}\r\n\r\n".encode()).replace("\n", "\n    "))
        if TCP_PORTS[port].startswith("RTSP"):
            print(f"\n  RTSP {port} says:\n    " +
                  _talk(ip, port, f"OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
                  .replace("\n", "\n    "))

    print("\n  ONVIF WS-Discovery (UDP 3702): ", end="")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            u.settimeout(3)
            u.sendto(WS_DISCOVERY.encode(), (ip, 3702))
            data, _ = u.recvfrom(8192)
            print("ANSWERED - this camera speaks ONVIF:\n", data[:600].decode("utf-8", "replace"))
    except OSError:
        print("no answer")

    print("\nVerdict:")
    if 554 in open_ports or 8554 in open_ports:
        print("  RTSP port open -> try source.type: rtsp with rtsp://IP:554/ (path may vary).")
    elif 6123 in open_ports:
        print("  Only the A9/V720 control port -> use source.type: a9v720 (no native local stream).")
    elif not open_ports:
        print("  Nothing open. Check the IP is right and the camera is on (ping it).")
        print("  If it is: normal for an A9/V720 on home WiFi -> use the a9-v720 fake server (README Step 1B).")
    else:
        print("  Some ports open but no RTSP - check the HTTP answers above for a /video or /mjpeg path.")

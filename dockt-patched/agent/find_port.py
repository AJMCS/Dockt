"""Print the first available local Dockt service port in the demo range."""
import socket
import sys


for port in range(8000, 8100):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            continue
        print(port)
        break
else:
    print("No free Dockt service port found in 8000-8099.", file=sys.stderr)
    raise SystemExit(1)

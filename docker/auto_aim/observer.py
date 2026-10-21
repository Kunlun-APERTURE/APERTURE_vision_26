"""Bounded, one-way diagnostics. No ground truth enters the bridge or algorithm."""
import json
from pathlib import Path
import queue
import socket
import socketserver
import struct
import threading


def receive(sock):
    def exact(size):
        data = bytearray()
        while len(data) < size:
            part = sock.recv(size-len(data))
            if not part:
                raise EOFError()
            data.extend(part)
        return bytes(data)
    h, b = struct.unpack('!II', exact(8))
    if not 0 < h <= 65536 or b > 16*1024*1024:
        raise ValueError('Invalid observer envelope')
    header = json.loads(exact(h))
    if header.get('version') != 1:
        raise ValueError('Unsupported observer version')
    return header, exact(b)


def send(sock, header, image=b''):
    data = json.dumps(dict(header, version=1),allow_nan=False,separators=(',',':')).encode()
    if len(data) > 65536 or len(image) > 16*1024*1024:
        raise ValueError('Observer envelope too large')
    sock.sendall(struct.pack('!II',len(data),len(image))+data+image)


class ObserverHub:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.subscribers = set()
        self.sequence = 0
        self.dropped = 0
        self.path = Path(path)
        self.path.unlink(missing_ok=True)
        hub = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(3)
                pending = queue.Queue(maxsize=8)
                try:
                    header, payload = receive(self.request)
                    if header.get('type') != 'observe' or payload:
                        return
                    with hub.lock:
                        hub.subscribers.add(pending)
                    send(self.request, {'type':'ready'})
                    while True:
                        try:
                            packet, jpeg = pending.get(timeout=.5)
                        except queue.Empty:
                            send(self.request, {'type':'heartbeat'})
                            continue
                        send(self.request, packet, jpeg)
                except (OSError,EOFError,ValueError):
                    pass
                finally:
                    with hub.lock:
                        hub.subscribers.discard(pending)

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.server = Server(str(self.path), Handler)
        # The containing runtime directory is private (0700). The evaluator
        # runs as the host user while the existing bridge runs as root.
        self.path.chmod(0o666)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()

    def publish(self, metadata, jpeg):
        with self.lock:
            self.sequence += 1
            packet = dict(metadata, type='observation', observer_seq=self.sequence,
                          observer_dropped=self.dropped)
            for subscriber in self.subscribers:
                try:
                    subscriber.put_nowait((packet,jpeg))
                except queue.Full:
                    self.dropped += 1

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.path.unlink(missing_ok=True)

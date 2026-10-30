"""Loopback-only test fixture. Contains no instrument recordings or captures."""
from contextlib import contextmanager
from io import StringIO
import csv
import re
import socketserver
import threading
import time

ROOT = "[INTERNAL]:\\"
SESSION = ROOT + "RTAUTOsession01"
SYNTHETIC_FILES = {
    SESSION+"\\dSTA00000\\00000000.STA": b"synthetic state bytes\x00\n",
    SESSION+"\\dTRC00000\\00000000.TRC": b"synthetic trace one\xff\n\x00",
    SESSION+"\\dTRC00000\\00000001.TRC": b"synthetic trace two\x00,\r\n",
}


class FakeInstrument:
    def __init__(self):
        self.files = dict(SYNTHETIC_FILES)
        self.commands = []; self.catalog_calls = {}
        self.fragment = False; self.fault = None; self.changing = False; self.delay = 0
        self.dirs = {ROOT, SESSION, SESSION+"\\dSTA00000", SESSION+"\\dTRC00000"}

    def listing(self, path):
        self.catalog_calls[path] = self.catalog_calls.get(path, 0)+1
        prefix = path if path.endswith("\\") else path+"\\"
        entries = []
        for folder in self.dirs:
            tail = folder[len(prefix):] if folder.startswith(prefix) else ""
            if tail and "\\" not in tail:
                entries.append(f"[{tail}],,0")
        for name, data in self.files.items():
            tail = name[len(prefix):] if name.startswith(prefix) else ""
            if tail and "\\" not in tail:
                entries.append(f"{tail},,{len(data)}")
        if self.changing and path == SESSION and self.catalog_calls[path] > 1:
            entries.append("new-file.dat,,1")
        stream = StringIO(); csv.writer(stream, lineterminator="\n").writerow([1000, 1000000]+sorted(entries))
        return stream.getvalue().encode()


@contextmanager
def instrument():
    state = FakeInstrument()
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            try:
                for line in self.rfile:
                    command = line.decode().strip(); state.commands.append(command)
                    if state.delay: time.sleep(state.delay)
                    if command == "*IDN?": response = b"Keysight Technologies,N9917B,TEST_ONLY,A.TEST\n"
                    elif command.startswith('MMEM:CAT? "'):
                        response = state.listing(command[len('MMEM:CAT? "'):-1])
                    elif command.startswith('MMEM:DATA? "'):
                        path = command[len('MMEM:DATA? "'):-1]
                        if path not in state.files: response = b'-256,"File not found"\n'
                        else:
                            data = state.files[path]; digits = str(len(data)).encode()
                            response = b"#"+str(len(digits)).encode()+digits+data+b"\n"
                            if state.fault == "truncated":
                                self.request.sendall(b"#210abc"); return
                            if state.fault == "indefinite": response = b"#0data\n"
                            if state.fault == "length": response = b"#1Xdata\n"
                            if state.fault == "terminator": response = response[:-1]+b"!"
                    else: response = b'-113,"Undefined header"\n'
                    if state.fragment:
                        for byte in response: self.request.sendall(bytes([byte]))
                    else: self.request.sendall(response)
            except OSError:
                pass
    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    thread.start(); state.port = server.server_address[1]
    try: yield state
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
        assert not thread.is_alive()

"""Tkinter file browser; also runnable directly from an IDE."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import json
import queue
import sys
import threading
import time

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "fieldfox_browser"

from .client import BrowserError, Cancelled, DRIVES, FieldFoxClient, RemotePath, component


class BrowserApp:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        self.root = root; self.busy = False; self.closing = False
        self.results = queue.Queue(); self.cancel = threading.Event(); self.last_report = 0
        self.entries = {}; self.catalog = None; self.loaded = None
        self.host = tk.StringVar(); self.port = tk.StringVar(value="5025")
        self.timeout = tk.StringVar(value="10"); self.drive = tk.StringVar(value="INTERNAL")
        self.path = tk.StringVar(value="[INTERNAL]:\\"); self.filter = tk.StringVar()
        self.identity = tk.StringVar(value="Enter the FieldFox IP address or hostname.")
        self.status = tk.StringVar(value="Browse instrument files, then download a file or complete session folder.")
        root.title("FieldFox Ethernet file browser"); root.geometry("1040x650"); root.minsize(820, 500)
        frame = ttk.Frame(root, padding=10); frame.pack(fill="both", expand=True)
        connection = ttk.Frame(frame); connection.pack(fill="x")
        self.inputs = []; self.buttons = []
        for text, variable, width in [("Instrument IP / hostname", self.host, 28), ("Port", self.port, 7), ("Timeout (s)", self.timeout, 6)]:
            ttk.Label(connection, text=text).pack(side="left", padx=(0, 5))
            widget = ttk.Entry(connection, textvariable=variable, width=width); widget.pack(side="left", padx=(0, 10)); self.inputs.append(widget)
        self._button(connection, "Connect / browse", self.browse).pack(side="left")
        ttk.Label(frame, textvariable=self.identity).pack(fill="x", pady=(8, 6))
        navigation = ttk.Frame(frame); navigation.pack(fill="x")
        storage = ttk.Combobox(navigation, textvariable=self.drive, values=DRIVES, state="readonly", width=12)
        storage.pack(side="left", padx=(0, 7)); self.inputs.append(storage); self.storage = storage
        storage.bind("<<ComboboxSelected>>", lambda _: self.path.set(f"[{self.drive.get()}]:\\"))
        path_entry = ttk.Entry(navigation, textvariable=self.path); path_entry.pack(side="left", fill="x", expand=True, padx=(0, 7)); self.inputs.append(path_entry)
        path_entry.bind("<Return>", lambda _: self.browse())
        self._button(navigation, "Go / refresh", self.browse).pack(side="left", padx=(0, 7))
        self._button(navigation, "Up", self.up).pack(side="left")
        filtering = ttk.Frame(frame); filtering.pack(fill="x", pady=8)
        ttk.Label(filtering, text="Filter names").pack(side="left")
        ttk.Entry(filtering, textvariable=self.filter).pack(side="left", fill="x", expand=True, padx=8)
        self.filter.trace_add("write", lambda *_: self._render())
        table = ttk.Frame(frame); table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("kind", "bytes", "type"), selectmode="browse")
        for name, title, width in [("#0", "Name", 520), ("kind", "Kind", 80), ("bytes", "Bytes", 130), ("type", "Type", 90)]:
            self.tree.heading(name, text=title); self.tree.column(name, width=width, anchor="e" if name == "bytes" else "w")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set); self.tree.pack(side="left", fill="both", expand=True); scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.open_selected)
        actions = ttk.Frame(frame); actions.pack(fill="x", pady=8)
        self._button(actions, "Download selected", self.download_selected).pack(side="left", padx=(0, 8))
        self._button(actions, "Download current folder", self.download_current).pack(side="left", padx=(0, 8))
        self._button(actions, "Save listing JSON", self.save_listing).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Cancel", command=self.cancel.set).pack(side="right")
        ttk.Label(frame, textvariable=self.status, wraplength=1000).pack(fill="x")
        root.protocol("WM_DELETE_WINDOW", self.close); root.after(100, self._poll)

    def _button(self, parent, text, command):
        from tkinter import ttk
        button = ttk.Button(parent, text=text, command=command); self.buttons.append(button); return button

    def _settings(self):
        client = FieldFoxClient(self.host.get().strip(), int(self.port.get()), float(self.timeout.get()))
        return client, RemotePath.parse(self.path.get())

    def _key(self, client, path):
        return (client.host, client.port, client.timeout, str(path))

    def _error(self, error):
        from tkinter import messagebox
        self.status.set(str(error)); messagebox.showerror("FieldFox browser", str(error), parent=self.root)

    def _report(self, text):
        now = time.monotonic()
        if now-self.last_report >= .1:
            self.last_report = now; self.results.put(("progress", text, None))

    def _job(self, work, done, message):
        if self.busy:
            self.status.set("An operation is already running."); return
        self.cancel.clear(); self.busy = True; self.last_report = 0; self.status.set(message)
        for widget in self.inputs + self.buttons:
            widget.configure(state="disabled")
        def worker():
            try: self.results.put((done, work(), None))
            except Exception as error: self.results.put((done, None, error))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        while True:
            try: callback, value, error = self.results.get_nowait()
            except queue.Empty: break
            if callback == "progress":
                self.status.set(value); continue
            self.busy = False
            for widget in self.inputs + self.buttons:
                widget.configure(state="normal")
            self.storage.configure(state="readonly")
            if not self.closing:
                if isinstance(error, Cancelled): self.status.set(str(error))
                elif error is not None: self._error(error)
                else:
                    try: callback(value)
                    except Exception as failure: self._error(failure)
        if self.closing and not self.busy:
            self.root.destroy(); return
        self.root.after(100, self._poll)

    def browse(self):
        try: client, path = self._settings()
        except (ValueError, OSError) as error: self._error(error); return
        def done(result):
            identity, self.catalog = result
            self.identity.set(identity); self.path.set(str(path)); self.loaded = self._key(client, path)
            self._render()
            self.status.set(f"{len(self.catalog.entries)} entries. Storage free: {self.catalog.free_bytes:,} bytes.")
        self._job(lambda: client.browse(path, self.cancel), done, "Connecting and listing folder…")

    def _render(self):
        self.tree.delete(*self.tree.get_children()); self.entries = {}
        if self.catalog is None: return
        query = self.filter.get().casefold()
        for number, entry in enumerate(self.catalog.entries):
            if query not in entry.name.casefold(): continue
            iid = str(number); self.entries[iid] = entry
            self.tree.insert("", "end", iid=iid, text=entry.name, values=("Folder" if entry.is_dir else "File", "" if entry.size is None or entry.is_dir else f"{entry.size:,}", entry.file_type))

    def _ready(self):
        if self.busy: return None
        try:
            client, path = self._settings()
            if self.catalog is None or self.loaded != self._key(client, path):
                raise BrowserError("Click Connect / browse to apply the current host and folder first.")
            return client, path
        except (ValueError, OSError) as error:
            self._error(error); return None

    def up(self):
        if self.busy: return
        try: self.path.set(str(RemotePath.parse(self.path.get()).parent)); self.browse()
        except BrowserError as error: self._error(error)

    def open_selected(self, event=None):
        ready = self._ready(); selection = self.tree.selection()
        if ready is None or not selection: return
        entry = self.entries[selection[0]]
        if entry.is_dir:
            self.path.set(str(ready[1].child(entry.name))); self.browse()

    def _folder_download(self, client, remote):
        from tkinter import filedialog, simpledialog
        parent = filedialog.askdirectory(title="Choose the parent directory for a NEW local folder", parent=self.root)
        if not parent: return
        name = simpledialog.askstring("Local folder", "New folder name", initialvalue=remote.name, parent=self.root)
        if name is None: return
        try: target = Path(parent) / component(name, local=True)
        except BrowserError as error: self._error(error); return
        self._job(lambda: client.download_folder(remote, target, cancel=self.cancel, progress=self._report),
                  lambda r: self.status.set(f"Saved {r['file_count']} files ({r['bytes']:,} bytes) to {r['destination']}. Open that folder in the separate decoder to plot it."), "Planning folder download…")

    def download_selected(self):
        from tkinter import filedialog
        ready = self._ready(); selection = self.tree.selection()
        if ready is None: return
        if not selection: self.status.set("Select a file or folder first."); return
        client, path = ready; entry = self.entries[selection[0]]; remote = path.child(entry.name)
        if entry.is_dir: self._folder_download(client, remote); return
        target = filedialog.asksaveasfilename(title="Download file to a NEW local filename", initialfile=entry.name, confirmoverwrite=False, parent=self.root)
        if target:
            self._job(lambda: client.download_file(remote, target, expected_size=entry.size, cancel=self.cancel, progress=self._report),
                      lambda r: self.status.set(f"Saved {r['bytes']:,} bytes to {target}; SHA-256 {r['sha256']}"), "Downloading file…")

    def download_current(self):
        ready = self._ready()
        if ready is not None: self._folder_download(*ready)

    def save_listing(self):
        from tkinter import filedialog
        if self._ready() is None: return
        target = filedialog.asksaveasfilename(title="Save folder listing", defaultextension=".json", confirmoverwrite=False, parent=self.root)
        if not target: return
        try:
            with open(target, "x", encoding="utf-8") as stream:
                json.dump({"path": self.path.get(), "identity": self.identity.get(), **asdict(self.catalog)}, stream, indent=2, ensure_ascii=False)
            self.status.set(f"Saved {target}")
        except OSError as error: self._error(error)

    def close(self):
        self.closing = True; self.cancel.set()
        if not self.busy: self.root.destroy()
        else: self.status.set("Stopping transfer and cleaning temporary files; waiting for the socket timeout if needed…")


def main():
    try:
        import tkinter as tk
    except ImportError as error:
        raise BrowserError("The GUI needs a Python installation with Tkinter; the CLI uses only the standard library.") from error
    root = tk.Tk(); BrowserApp(root); root.mainloop(); return 0


if __name__ == "__main__":
    raise SystemExit(main())

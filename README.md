# fieldfox_ethernet_browser

A small Python/Tkinter browser for files stored on a Keysight FieldFox N9917B,
accessed over Ethernet. Browse internal memory, SD cards and USB storage;
download individual files or complete recording-session folders while
preserving the original STA/TRC directory layout.

This is separate from [testing_data_conversion](https://github.com/NicolasSayed/testing_data_conversion),
which decodes and plots recordings after they have been copied locally.

**Status:** 35 tests pass against a loopback-only simulated instrument, including
the GUI and CLI. No live FieldFox connection has been tested here. The protocol
comes from Keysight's FieldFox documentation and static inspection of the supplied
Data Link file-access paths; firmware-specific behavior still needs a hardware check.

## Open the browser

Requires Python 3.11+ with Tkinter. There are no third-party runtime dependencies.
From the downloaded/cloned repository:

```bash
python -m fieldfox_browser gui
```

Or run `python scripts/fieldfox_browser.py`. Directly running
`fieldfox_browser/gui.py` from an IDE works too. Keep the whole repository
together. On Windows, `py -3.11` can replace `python`.

Optional installation adds command launchers:

```bash
python -m pip install .
fieldfox-file-browser
```

`fieldfox-file-browser` opens the GUI without a console on Windows.
`fieldfox-browser` provides the CLI.

## Browse and copy a session

1. Connect the computer and FieldFox to a reachable Ethernet network. Enter the
   instrument's IP address or hostname in the window. The socket port defaults
   to **5025** and the inactivity timeout to 10 seconds.
2. Click **Connect / browse**. The instrument identity and storage contents appear.
   Choose INTERNAL, SDCARD, USBDISK, USBDISK1 or USBDISK2, then click **Go / refresh**.
3. Double-click folders to navigate. **Up** goes to the parent. The name filter
   filters the displayed listing locally. The path field accepts a complete path.
4. Select a file/folder and click **Download selected**, or browse into a session
   and click **Download current folder**. For a folder, choose a local parent and
   a new folder name. Existing destinations are refused.

For example, navigate to the storage containing `RecordPlayback\GPSA` and select
the appropriate `RTAUTOsessionXX`. The actual directory location/name may differ.
A complete folder copy includes its STA/TRC partitions, RPX files and other files
listed by the instrument. No decoder metadata files are inserted into that copy.

The **Cancel** button stops an operation. Closing during a transfer requests
cancellation and waits for the worker to clean up; an outstanding network read
may take up to the configured socket timeout. Progress is throttled and network
work runs outside the Tk event loop. No connection or network scan occurs at startup.

**Save listing JSON** saves the current entries, original catalogue response and
instrument identity locally, which can help diagnose firmware differences.

After copying, open the new local session folder in the separate decoder GUI:

```bash
python -m fieldfox_recording gui
```

That command requires the decoder repository/package to be installed separately.

## Command line

Replace the example IP `192.0.2.10` with your instrument's address. Single-quoted
paths below work in PowerShell and Bash:

```bash
python -m fieldfox_browser idn 192.0.2.10
python -m fieldfox_browser ls 192.0.2.10 '[INTERNAL]:'
python -m fieldfox_browser ls 192.0.2.10 '[SDCARD]:\RecordPlayback\GPSA' --json
python -m fieldfox_browser download 192.0.2.10 '[SDCARD]:\RecordPlayback\GPSA\RTAUTOsession01' --recursive --output downloads/RTAUTOsession01
python -m fieldfox_browser download 192.0.2.10 '[INTERNAL]:\example.csv' --output downloads/example.csv
```

Use `--port` and `--timeout` on any network command. To inspect an unrecognized
catalogue without guessing its format, use `ls HOST PATH --raw`.
Downloads have configurable `--max-file-bytes` (default 1 GiB) and recursive
`--max-total-bytes` (default 100 GiB). Folder planning also limits the tree to
100,000 entries and 32 directory levels. Exit code 2 reports a connection,
protocol, path or filesystem error; Ctrl+C returns 130 after cleanup.

## Python API

```python
from fieldfox_browser import FieldFoxClient

client = FieldFoxClient('192.0.2.10', port=5025, timeout=10)
print(client.identify())
listing = client.catalog(r'[INTERNAL]:\RecordPlayback\GPSA')
for item in listing.entries:
    print(item.name, item.is_dir, item.size)

result = client.download_folder(
    r'[INTERNAL]:\RecordPlayback\GPSA\RTAUTOsession01',
    'downloads/RTAUTOsession01',
)
print(result['file_count'], result['bytes'])
```

`download_file` and `download_folder` also accept a `threading.Event` as `cancel`
and a `progress(message)` callback. Completed file results include SHA-256 hashes
of received bytes; these are local hashes, not instrument-provided checksums.

## Protocol and implementation workflow

Keysight documents a TCP SCPI socket on port 5025. This client uses `*IDN?` for
identity, `MMEM:CAT?` for directory listings and `MMEM:DATA?` for file reads.
See the official [socket example](https://helpfiles.keysight.com/csg/FFProgrammingHelpWebHelp/Examples/MATLAB/MATLAB_-_Sockets-_Binary_Block_File_Transfer.htm),
[catalogue query](https://helpfiles.keysight.com/csg/FFProgrammingHelpWebHelp/MMEMory_CATalog.htm)
and [file-read reference](https://helpfiles.keysight.com/csg/FFProgrammingHelpWebHelp/MMEMory_DATA.htm).

Storage aliases and bracketed directory names follow the
[FieldFox path reference](https://helpfiles.keysight.com/csg/FFProgrammingHelpWebHelp/MMEMory_CDIRectory.htm).
The parser accepts quoted `name,type,size` entries and flattened triples after
the used/free byte counts described in the
[memory examples](https://helpfiles.keysight.com/csg/FFProgrammingHelpWebHelp/Examples/Memory_Command_Examples.htm).
Unknown file types remain visible; the browser does not interpret recording contents.

The supplied Data Link 8.2.9762.18414 files independently corroborate these commands:
`FFoxDataLink.exe` type `y` uses catalogue queries in `g`/`k` and file reads in
`a`/`d`; its LAN manager uses port 5025. Those paths were inspected from existing
local ILSpy output. This implementation contains no proprietary source or runtime
dependency. No new bulk decompilation was needed.

Only file-read and listing queries are sent. The browser has no upload, delete,
rename, directory-change or measurement-control operations. Absolute paths avoid
changing the instrument's active directory. It connects only to the supplied host.

## Transfer behavior and limits

- Binary reads stream in 64 KiB chunks. They require a definite-length IEEE 488.2
  `#n` block, followed by LF or CRLF. Indefinite-length `#0` and unrecognized
  framing are rejected rather than read as guessed file contents.
- A file appears at the chosen destination only after its response is complete.
  Recursive copies use a temporary sibling directory; the final folder appears
  after all files and listing checks pass. Failures/cancellation remove staging.
- Folder copies check file lengths against the catalogue and recheck directory
  membership/sizes afterward. **Copy finished recording sessions.** Same-size
  content changes cannot be detected without a remote checksum; this is not an
  atomic snapshot of a running acquisition.
- Remote path traversal, SCPI string injection, unsafe local names and Windows
  case/name collisions are rejected. Some Linux filenames may therefore be
  browsable but unsuitable for a portable Windows folder copy.
- UTF-8 catalogue/filename encoding is currently supported. An older firmware
  encoding, inaccessible storage, permissions or a different catalogue/block
  format may need adjustment after observing that instrument's response.
- Both `[INTERNAL]:` and plain `INTERNAL:`-style aliases are accepted. If a
  firmware prefers a different supported spelling, edit the path in the GUI.

This is an instrument file browser, not an SMB/SFTP browser for another computer.
No supplied binaries, recordings or downloaded data are included. Keep downloaded
data outside source control; `downloads/` is ignored.

## Development checks

```bash
python -m pip install pytest
python -m pytest -q
```

The loopback fixture checks catalogue variants, fragmented blocks, embedded
binary newlines, empty files, partial/malformed transfers, limits, cancellation,
changed listings, destination preservation, CLI operations and invisible Tkinter
browse/download/export workflows. Mock fixture bytes are explicitly synthetic;
tests do not establish live N9917B compatibility.

"""Open the Ethernet browser from this checkout, including IDE Run File."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fieldfox_browser.gui import main

if __name__ == "__main__":
    raise SystemExit(main())

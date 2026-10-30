import json
from pathlib import Path
import os
import subprocess
import sys
import time

import pytest

from .fake_instrument import SESSION, instrument


def test_gui_browse_download_and_listing_export(tmp_path,monkeypatch):
    tk=pytest.importorskip('tkinter')
    from tkinter import filedialog,simpledialog
    from fieldfox_browser.gui import BrowserApp
    try:root=tk.Tk();root.withdraw()
    except tk.TclError:pytest.skip('No Tk display')
    app=BrowserApp(root);errors=[];monkeypatch.setattr(app,'_error',errors.append)
    def finish():
        deadline=time.monotonic()+5
        while app.busy and time.monotonic()<deadline:
            root.update();time.sleep(.01)
        assert not app.busy and not errors,errors
    try:
        with instrument() as fake:
            assert fake.commands==[]
            app.host.set('127.0.0.1');app.port.set(str(fake.port));app.timeout.set('1')
            app.browse();finish()
            assert len(app.tree.get_children())==1 and 'N9917B' in app.identity.get()
            app.tree.selection_set(app.tree.get_children()[0]);app.open_selected();finish()
            assert app.path.get()==SESSION and len(app.tree.get_children())==2
            monkeypatch.setattr(filedialog,'askdirectory',lambda **_:str(tmp_path))
            monkeypatch.setattr(simpledialog,'askstring',lambda *a,**k:'local-session')
            app.download_current();finish()
            assert (tmp_path/'local-session/dTRC00000/00000001.TRC').exists()
            target=tmp_path/'listing.json'
            monkeypatch.setattr(filedialog,'asksaveasfilename',lambda **_:str(target))
            app.save_listing();assert json.loads(target.read_text())['path']==SESSION
            app.filter.set('dSTA');assert len(app.tree.get_children())==1
    finally:app.close()


@pytest.mark.parametrize('script',['fieldfox_browser/gui.py','scripts/fieldfox_browser.py'])
def test_direct_gui_launch_from_another_directory(tmp_path,script):
    pytest.importorskip('tkinter')
    checkout=Path(__file__).resolve().parents[1]
    code="""import runpy,tkinter as tk
original=tk.Tk
def hidden():
    root=original();root.withdraw();root.after(150,root.destroy);return root
tk.Tk=hidden
"""+f"runpy.run_path({str(checkout/script)!r},run_name='__main__')"
    result=subprocess.run([sys.executable,'-c',code],cwd=tmp_path,env=os.environ|{'PYTHONPATH':''},capture_output=True,text=True,timeout=15)
    if 'no display name' in result.stderr:pytest.skip('No Tk display')
    assert result.returncode==0,result.stderr

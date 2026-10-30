from pathlib import Path
import hashlib
import json
import subprocess
import sys
import threading

import pytest

from fieldfox_browser.client import BrowserError, Cancelled, FieldFoxClient, RemotePath, parse_catalog
from .fake_instrument import ROOT, SESSION, SYNTHETIC_FILES, instrument


def test_catalogue_quoted_entries_and_names_with_commas():
    catalog = parse_catalog('10,200,"[RTAUTOsession01],,0","trace,name.TRC,,4"')
    assert catalog.used_bytes == 10 and catalog.free_bytes == 200
    assert catalog.entries[0].name == 'RTAUTOsession01' and catalog.entries[0].is_dir
    assert catalog.entries[1].name == 'trace,name.TRC' and catalog.entries[1].size == 4


def test_catalogue_flat_triples_and_unknown_directory_size():
    catalog = parse_catalog('10,200,[folder],,,"a,b.TRC",TRC,4')
    assert catalog.entries[0].is_dir and catalog.entries[0].size is None
    assert catalog.entries[1].name == 'a,b.TRC'
    assert parse_catalog('0,0').entries == ()


@pytest.mark.parametrize('text',['', 'not a catalogue', '0,0,"file,,bad"',
    '0,0,"../outside,,5"','0,0,"file,,1","file,,1"','0,0,"unterminated',
    '0,0,"a/b,,5"','-1,0','0,0,"file,,999999999999999999999"'])
def test_invalid_catalogue_is_not_guessed(text):
    with pytest.raises(BrowserError):parse_catalog(text)


def test_paths_preserve_storage_style_and_cannot_inject_commands():
    path=RemotePath.parse('[SDCARD]:/RecordPlayback/GPSA')
    assert str(path.child('RTAUTOsession01'))=='[SDCARD]:\\RecordPlayback\\GPSA\\RTAUTOsession01'
    assert str(path.parent)=='[SDCARD]:\\RecordPlayback'
    assert str(RemotePath.parse('INTERNAL:'))=='INTERNAL:\\'
    assert RemotePath.parse(ROOT).parent==RemotePath.parse(ROOT)
    for bad in ['relative/path','[INTERNAL]:\\..\\x','[INTERNAL]:\\a";*RST','[UNKNOWN]:\\','[INTERNAL]:\\x\n*RST']:
        with pytest.raises(BrowserError):RemotePath.parse(bad)


def test_identity_browse_fragmented_binary_and_hash(tmp_path):
    with instrument() as fake:
        fake.fragment=True
        client=FieldFoxClient('127.0.0.1',fake.port,1)
        identity,catalog=client.browse(ROOT)
        assert 'N9917B' in identity and catalog.entries[0].name=='RTAUTOsession01'
        remote,next_data=next(iter(SYNTHETIC_FILES.items()))
        target=tmp_path/'one.dat'
        result=client.download_file(remote,target,expected_size=len(next_data))
        assert target.read_bytes()==next_data
        assert result['sha256']==hashlib.sha256(next_data).hexdigest()
        assert all(c=='*IDN?' or c.startswith(('MMEM:CAT? ','MMEM:DATA? ')) for c in fake.commands)


@pytest.mark.parametrize('fault',['truncated','indefinite','length','terminator'])
def test_bad_blocks_do_not_publish_partial_files(tmp_path,fault):
    with instrument() as fake:
        fake.fault=fault
        target=tmp_path/'bad.dat'
        with pytest.raises(BrowserError):FieldFoxClient('127.0.0.1',fake.port,1).download_file(next(iter(fake.files)),target)
        assert not target.exists() and not list(tmp_path.glob('.fieldfox-*'))


def test_empty_file_and_two_blocks_on_same_connection(tmp_path):
    with instrument() as fake:
        remote=SESSION+'\\empty.dat';fake.files[remote]=b''
        client=FieldFoxClient('127.0.0.1',fake.port,1)
        with client.connection() as connection:
            connection.download(RemotePath.parse(remote),tmp_path/'empty',expected_size=0,max_bytes=100)
            path,data=next(iter(SYNTHETIC_FILES.items()))
            connection.download(RemotePath.parse(path),tmp_path/'other',expected_size=len(data),max_bytes=100)
        assert (tmp_path/'empty').read_bytes()==b'' and (tmp_path/'other').read_bytes()==data


def test_size_limits_and_existing_destinations(tmp_path):
    with instrument() as fake:
        client=FieldFoxClient('127.0.0.1',fake.port,1);remote=next(iter(fake.files))
        for options in [{'expected_size':999},{'max_bytes':1}]:
            with pytest.raises(BrowserError):client.download_file(remote,tmp_path/'missing',**options)
        target=tmp_path/'keep';target.write_bytes(b'keep')
        with pytest.raises(FileExistsError):client.download_file(remote,target)
        assert target.read_bytes()==b'keep'


def test_tree_copy_preserves_files_and_adds_no_metadata(tmp_path):
    with instrument() as fake:
        client=FieldFoxClient('127.0.0.1',fake.port,1);target=tmp_path/'RTAUTOsession01'
        result=client.download_folder(SESSION,target)
        assert result['file_count']==3 and result['bytes']==sum(map(len,SYNTHETIC_FILES.values()))
        expected={path[len(SESSION)+1:].replace('\\','/'):data for path,data in SYNTHETIC_FILES.items()}
        actual={p.relative_to(target).as_posix():p.read_bytes() for p in target.rglob('*') if p.is_file()}
        assert actual==expected
        assert all(Path(item['local_path']).is_file() for item in result['files'])
        assert not list(tmp_path.glob('.fieldfox-*'))
        with pytest.raises(FileExistsError):client.download_folder(SESSION,target)


@pytest.mark.parametrize('kind',['missing','file'])
def test_recursive_download_requires_a_listed_folder(tmp_path,kind):
    with instrument() as fake:
        path=SESSION+'\\missing' if kind=='missing' else next(iter(fake.files))
        with pytest.raises(BrowserError,match='accessible directory'):
            FieldFoxClient('127.0.0.1',fake.port,1).download_folder(path,tmp_path/'out')
        assert not (tmp_path/'out').exists()


@pytest.mark.parametrize('options',[{'max_entries':1},{'max_depth':1},{'max_total_bytes':1},{'max_file_bytes':1}])
def test_tree_limits_leave_no_partial_session(tmp_path,options):
    with instrument() as fake:
        remote=ROOT if 'max_depth' in options else SESSION
        with pytest.raises(BrowserError):FieldFoxClient('127.0.0.1',fake.port,1).download_folder(remote,tmp_path/'out',**options)
        assert not (tmp_path/'out').exists()


def test_changed_remote_listing_cleans_staging(tmp_path):
    with instrument() as fake:
        fake.changing=True
        with pytest.raises(BrowserError,match='changed during transfer'):
            FieldFoxClient('127.0.0.1',fake.port,1).download_folder(SESSION,tmp_path/'out')
        assert not (tmp_path/'out').exists() and not list(tmp_path.glob('.fieldfox-*'))


def test_cancelled_transfer_cleans_staging(tmp_path):
    with instrument() as fake:
        cancel=threading.Event();client=FieldFoxClient('127.0.0.1',fake.port,1)
        with pytest.raises(Cancelled):
            client.download_folder(SESSION,tmp_path/'out',cancel=cancel,progress=lambda _:cancel.set())
        assert not (tmp_path/'out').exists() and not list(tmp_path.glob('.fieldfox-*'))
        cancel.set()
        with pytest.raises(Cancelled):client.catalog(ROOT,cancel)


def test_mid_file_cancel_cleans_temporary_file(tmp_path):
    with instrument() as fake:
        remote=SESSION+'\\large.dat';fake.files[remote]=b'x'*150000;cancel=threading.Event()
        with pytest.raises(Cancelled):
            FieldFoxClient('127.0.0.1',fake.port,1).download_file(remote,tmp_path/'out',cancel=cancel,progress=lambda _:cancel.set())
        assert not (tmp_path/'out').exists() and not list(tmp_path.glob('.fieldfox-*'))


def test_case_collisions_rejected_for_windows_download(tmp_path):
    with instrument() as fake:
        fake.files[SESSION+'\\A.dat']=b'a';fake.files[SESSION+'\\a.dat']=b'b'
        with pytest.raises(BrowserError,match='collide'):
            FieldFoxClient('127.0.0.1',fake.port,1).download_folder(SESSION,tmp_path/'out')


def test_socket_timeout_is_bounded():
    with instrument() as fake:
        fake.delay=.15
        with pytest.raises(TimeoutError):FieldFoxClient('127.0.0.1',fake.port,.03).identify()


def test_cli_lists_and_downloads_against_loopback(tmp_path):
    with instrument() as fake:
        base=[sys.executable,'-m','fieldfox_browser']
        listing=subprocess.run(base+['ls','127.0.0.1',ROOT,'--port',str(fake.port),'--json'],capture_output=True,text=True,timeout=10)
        assert listing.returncode==0,listing.stderr
        assert json.loads(listing.stdout)['entries'][0]['name']=='RTAUTOsession01'
        copy=subprocess.run(base+['download','127.0.0.1',SESSION,'--port',str(fake.port),'--recursive','--output',str(tmp_path/'copy')],capture_output=True,text=True,timeout=10)
        assert copy.returncode==0,copy.stderr
        assert '3 files' in copy.stdout

"""Existing .tmp saves survive transient locks without repeating scientific work."""
import ctypes
import errno
import json
import os
from pathlib import Path
from threading import Timer

import pytest

from backend.shared import artifacts, storage
from backend.shared.artifacts import save_rows, save_json
from backend.shared.storage import read_jsonl


def locked_error(code=5):
    error=PermissionError(errno.EACCES,'Temporary sharing lock')
    error.winerror=code
    return error


@pytest.mark.parametrize('code',[5,32,33])
def test_replace_retries_same_existing_temporary_without_touching_old_snapshot(tmp_path,monkeypatch,code):
    path=tmp_path/'candidates.jsonl'
    save_rows(path,[{'id':'old'}])
    replace=Path.replace
    calls=[]
    def sharing_lock(source,target):
        calls.append(source)
        assert source==path.with_name('candidates.jsonl.tmp')
        assert read_jsonl(source)==[{'id':'new'}]
        if len(calls)<3:
            assert read_jsonl(path)==[{'id':'old'}]
            raise locked_error(code)
        return replace(source,target)
    monkeypatch.setattr(Path,'replace',sharing_lock)
    monkeypatch.setattr(artifacts.time,'sleep',lambda _:None)
    save_rows(path,[{'id':'new'}])
    assert len(calls)==3 and read_jsonl(path)==[{'id':'new'}]
    assert not path.with_name('candidates.jsonl.tmp').exists()


def test_persistent_lock_has_bounded_retries_and_keeps_complete_old_and_pending_data(tmp_path,monkeypatch):
    path=tmp_path/'snapshot.json'
    save_json(path,{'old':True})
    calls=[]
    def locked(source,target):
        calls.append(source)
        raise locked_error()
    monkeypatch.setattr(Path,'replace',locked)
    monkeypatch.setattr(artifacts.time,'sleep',lambda _:None)
    with pytest.raises(PermissionError): save_json(path,{'new':True})
    assert len(calls)==len(artifacts._REPLACE_DELAYS)+1
    assert json.loads(path.read_text())=={'old':True}
    assert json.loads(path.with_name('snapshot.json.tmp').read_text())=={'new':True}


def test_unrelated_io_failure_is_not_retried(tmp_path,monkeypatch):
    calls=[]
    def disk_full(source,target):
        calls.append(source)
        raise OSError(errno.ENOSPC,'Disk full')
    monkeypatch.setattr(Path,'replace',disk_full)
    with pytest.raises(OSError,match='Disk full'): save_rows(tmp_path/'snapshot.jsonl',[])
    assert len(calls)==1


def test_reader_releases_handle_before_parsing_and_returns_one_consistent_snapshot(tmp_path,monkeypatch):
    path=tmp_path/'snapshot.jsonl'
    save_rows(path,[{'old':1},{'old':2}])
    replacement=tmp_path/'replacement.jsonl'
    save_rows(replacement,[{'new':3}])
    loads=json.loads
    replaced=[]
    def parsing(line):
        if not replaced:
            replacement.replace(path)  # Windows refuses this if read_jsonl still holds its handle.
            replaced.append(True)
        return loads(line)
    monkeypatch.setattr(storage.json,'loads',parsing)
    assert read_jsonl(path)==[{'old':1},{'old':2}]
    assert read_jsonl(path)==[{'new':3}]


@pytest.mark.skipif(os.name!='nt',reason='Windows sharing flags')
def test_real_windows_lock_clears_without_aborting_checkpoint(tmp_path):
    from ctypes import wintypes
    path=tmp_path/'snapshot.jsonl'
    save_rows(path,[{'old':True}])
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,
                                wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateFileW(str(path),0x80000000,3,None,3,0,None)
    assert handle!=ctypes.c_void_p(-1).value
    release=Timer(.15,lambda:kernel.CloseHandle(handle))
    release.start()
    try:
        save_rows(path,[{'new':True}])
    finally:
        release.join()
    assert read_jsonl(path)==[{'new':True}]

"""OpenCode credentials: environment override or current-user Windows vault."""
import ctypes
import os
from ctypes import wintypes


_TARGET = 'crop-to-human-health/OpenCodeInference'


class _Credential(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]


def api_key():
    """Never include the result in settings, request metrics or exception text."""
    override = os.getenv('OPENCODE_API_KEY', '').strip()
    if override:
        return override
    if os.name != 'nt':
        return ''
    vault = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
    vault.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.POINTER(ctypes.POINTER(_Credential))]
    vault.CredReadW.restype = wintypes.BOOL
    vault.CredFree.argtypes = [ctypes.c_void_p]
    pointer = ctypes.POINTER(_Credential)()
    if not vault.CredReadW(_TARGET, 1, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == 1168:  # No credential configured.
            return ''
        raise RuntimeError('Could not read the OpenCode credential from Windows Credential Manager')
    try:
        row = pointer.contents
        return ctypes.string_at(row.CredentialBlob, row.CredentialBlobSize).decode('utf-8')
    finally:
        vault.CredFree(pointer)


def save_api_key(value):
    """Store a credential for this Windows user, outside the repository."""
    if os.name != 'nt':
        raise RuntimeError('Set OPENCODE_API_KEY in the backend environment on this platform')
    value = value.strip()
    if not value or any(char.isspace() for char in value):
        raise ValueError('The API key must be nonempty and contain no whitespace')
    encoded = value.encode('utf-8')
    blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
    row = _Credential(Type=1, TargetName=_TARGET, CredentialBlobSize=len(encoded),
                      CredentialBlob=blob, Persist=2, UserName='OpenCode')
    vault = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
    vault.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    vault.CredWriteW.restype = wintypes.BOOL
    if not vault.CredWriteW(ctypes.byref(row), 0):
        raise RuntimeError('Could not save the OpenCode credential in Windows Credential Manager')

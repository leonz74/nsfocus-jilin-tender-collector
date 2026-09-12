"""Native Mac Keychain storage; secrets never pass through command arguments."""
from __future__ import annotations

import ctypes as C
import hashlib
import json
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit, urlunsplit


def key_account(config_path: Path, ai: dict) -> str:
    parsed = urlsplit(str(ai.get("endpoint", "")).strip())
    endpoint = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
                          parsed.path.rstrip("/"), parsed.query, ""))
    scope = [str(config_path.resolve()), ai.get("protocol", "openai_compatible"), endpoint]
    return hashlib.sha256(json.dumps(scope).encode()).hexdigest()


class MacKeyStore:
    service = b"cn.tender-downloader.ai-api-key"
    supported = sys.platform == "darwin"
    _lock = threading.RLock()

    def _native(self):
        if not self.supported:
            raise ValueError("保存 Key 暂支持 Mac 钥匙串；当前系统可使用临时 Key 或环境变量")
        if hasattr(self, "_security"):
            return self._security, self._cf
        security = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
        cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        security.SecKeychainFindGenericPassword.argtypes = [C.c_void_p, C.c_uint32, C.c_char_p, C.c_uint32, C.c_char_p, C.POINTER(C.c_uint32), C.POINTER(C.c_void_p), C.POINTER(C.c_void_p)]
        security.SecKeychainAddGenericPassword.argtypes = [C.c_void_p, C.c_uint32, C.c_char_p, C.c_uint32, C.c_char_p, C.c_uint32, C.c_void_p, C.POINTER(C.c_void_p)]
        security.SecKeychainItemModifyAttributesAndData.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32, C.c_void_p]
        security.SecKeychainItemDelete.argtypes = [C.c_void_p]
        security.SecKeychainItemFreeContent.argtypes = [C.c_void_p, C.c_void_p]
        cf.CFRelease.argtypes = [C.c_void_p]
        cf.CFRelease.restype = None
        self._security, self._cf = security, cf
        return security, cf

    @staticmethod
    def _check(status: int) -> None:
        if status:
            raise ValueError(f"Mac 钥匙串操作失败（状态 {status}），请检查钥匙串是否已解锁或允许访问")

    def _find(self, account: str, *, read: bool = False):
        security, _ = self._native()
        name = account.encode()
        length, data, item = C.c_uint32(), C.c_void_p(), C.c_void_p()
        status = security.SecKeychainFindGenericPassword(None, len(self.service), self.service,
            len(name), name, C.byref(length) if read else None,
            C.byref(data) if read else None, C.byref(item))
        if status == -25300:  # errSecItemNotFound
            return None, ""
        self._check(status)
        try:
            value = C.string_at(data, length.value).decode("utf-8") if read else ""
        finally:
            if data.value:
                security.SecKeychainItemFreeContent(None, data)
        return item, value

    def exists(self, account: str) -> bool:
        if not self.supported:
            return False
        with self._lock:
            item, _ = self._find(account)
            if item:
                self._cf.CFRelease(item)
            return bool(item)

    def get(self, account: str) -> str:
        if not self.supported:
            return ""
        with self._lock:
            item, value = self._find(account, read=True)
            if item:
                self._cf.CFRelease(item)
            return value

    def save(self, account: str, credential: str) -> None:
        with self._lock:
            security, cf = self._native()
            item, _ = self._find(account)
            encoded, name = credential.encode(), account.encode()
            if item:
                try:
                    self._check(security.SecKeychainItemModifyAttributesAndData(item, None, len(encoded), encoded))
                finally:
                    cf.CFRelease(item)
            else:
                self._check(security.SecKeychainAddGenericPassword(None, len(self.service), self.service,
                    len(name), name, len(encoded), encoded, None))

    def delete(self, account: str) -> None:
        with self._lock:
            security, cf = self._native()
            item, _ = self._find(account)
            if item:
                try:
                    self._check(security.SecKeychainItemDelete(item))
                finally:
                    cf.CFRelease(item)

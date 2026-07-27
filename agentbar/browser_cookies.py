"""Best-effort import of corp cookies from local Chromium profiles.

The importer only runs after the user clicks the panel button. It reads local
browser cookie databases, builds a Cookie header for the requested host, and
returns explicit errors when browser storage or Keychain access blocks us.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


CHROME_EPOCH_OFFSET = 11_644_473_600


@dataclass
class ImportedCookie:
    header: str
    source: str
    count: int


class CookieImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserProfile:
    name: str
    safe_storage_service: str
    safe_storage_account: str
    cookie_db: Path


_BROWSERS = (
    ("Chrome", "Chrome Safe Storage", "Chrome",
     Path("~/Library/Application Support/Google/Chrome")),
    ("Chrome Beta", "Chrome Safe Storage", "Chrome",
     Path("~/Library/Application Support/Google/Chrome Beta")),
    ("Microsoft Edge", "Microsoft Edge Safe Storage", "Microsoft Edge",
     Path("~/Library/Application Support/Microsoft Edge")),
    ("Brave", "Brave Safe Storage", "Brave",
     Path("~/Library/Application Support/BraveSoftware/Brave-Browser")),
)


def _candidate_profiles() -> list[BrowserProfile]:
    profiles: list[BrowserProfile] = []
    for browser, service, account, root in _BROWSERS:
        base = root.expanduser()
        if not base.exists():
            continue
        for prof in [base / "Default", *sorted(base.glob("Profile *"))]:
            for db in (prof / "Network" / "Cookies", prof / "Cookies"):
                if db.exists():
                    profiles.append(BrowserProfile(
                        name=f"{browser} / {prof.name}",
                        safe_storage_service=service,
                        safe_storage_account=account,
                        cookie_db=db,
                    ))
                    break
    return profiles


def _chrome_expiry_valid(expires_utc: int | None) -> bool:
    if not expires_utc:
        return True
    # Chrome stores microseconds since 1601-01-01 UTC.
    return expires_utc / 1_000_000 - CHROME_EPOCH_OFFSET > time.time()


def _domain_matches(cookie_host: str, target_host: str) -> bool:
    h = (cookie_host or "").lstrip(".").lower()
    t = target_host.lower()
    return t == h or t.endswith("." + h)


def _read_safe_storage_password(profile: BrowserProfile) -> str | None:
    security = shutil.which("security") or "/usr/bin/security"
    try:
        r = subprocess.run(
            [
                security,
                "find-generic-password",
                "-w",
                "-s",
                profile.safe_storage_service,
                "-a",
                profile.safe_storage_account,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    password = r.stdout.strip()
    return password or None


def _openssl_decrypt(ciphertext: bytes, password: str) -> str | None:
    if not ciphertext:
        return None
    data = ciphertext[3:] if ciphertext.startswith((b"v10", b"v11")) else ciphertext
    key = hashlib.pbkdf2_hmac(
        "sha1", password.encode("utf-8"), b"saltysalt", 1003, dklen=16
    )
    openssl = shutil.which("openssl") or "/usr/bin/openssl"
    try:
        r = subprocess.run(
            [
                openssl,
                "enc",
                "-aes-128-cbc",
                "-d",
                "-K",
                key.hex(),
                "-iv",
                (b" " * 16).hex(),
            ],
            input=data,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        return r.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _copy_cookie_db(path: Path) -> Path:
    fd, tmp_name = tempfile.mkstemp(prefix="agentbar-cookies-", suffix=".sqlite")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(path, tmp)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise CookieImportError(f"无法读取浏览器 Cookie 数据库: {e}") from e
    return tmp


def _cookies_from_profile(profile: BrowserProfile, target_host: str) -> ImportedCookie | None:
    tmp = _copy_cookie_db(profile.cookie_db)
    password: str | None = None
    cookies: list[tuple[str, str, str]] = []
    try:
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            rows = con.execute(
                """
                SELECT host_key, name, value, encrypted_value, expires_utc
                  FROM cookies
                 WHERE name != ''
                """
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    finally:
        tmp.unlink(missing_ok=True)

    for host_key, name, value, encrypted, expires_utc in rows:
        if not _domain_matches(host_key, target_host):
            continue
        if not _chrome_expiry_valid(expires_utc):
            continue
        val = value or ""
        if not val and encrypted:
            if password is None:
                password = _read_safe_storage_password(profile)
            if not password:
                continue
            val = _openssl_decrypt(bytes(encrypted), password) or ""
        if val:
            cookies.append((str(host_key), str(name), val))

    if not cookies:
        return None
    cookies.sort(key=lambda x: (len(x[0].lstrip(".")), x[1]), reverse=True)
    seen: set[str] = set()
    parts: list[str] = []
    for _, name, val in cookies:
        if name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={val}")
    return ImportedCookie(
        header="; ".join(parts),
        source=f"{profile.name} ({profile.cookie_db})",
        count=len(parts),
    )


def import_cookie_header(target_host: str) -> ImportedCookie:
    profiles = _candidate_profiles()
    if not profiles:
        raise CookieImportError("未找到 Chrome / Edge / Brave 的本地 Cookie 数据库")

    encrypted_seen = False
    for profile in profiles:
        result = _cookies_from_profile(profile, target_host)
        if result:
            return result
        # A profile can contain only encrypted cookies; keep the final error
        # actionable when Keychain access was denied.
        encrypted_seen = True
    if encrypted_seen:
        raise CookieImportError(
            "未从浏览器读到可用 Cookie；请确认已在 Chrome/Edge 登录，"
            "并允许读取浏览器 Safe Storage Keychain"
        )
    raise CookieImportError(f"未找到 {target_host} 的 Cookie")

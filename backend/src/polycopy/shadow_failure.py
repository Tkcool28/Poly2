"""Denial-only durable latch when the signed ERROR sink itself is unavailable.

This sidecar cannot authorize any evidence or replace a signed journal record.
Any presence (including malformed/torn bytes) forbids reopening this generation.
If storage cannot retain even this latch, diagnostics remain failed and an operator
must preserve the failed installation; physical storage loss is not recoverable.
"""
import json
import logging
import os
import sqlite3
import stat
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def failed(directory):
    path = directory / "capture-failed.json"
    return path.exists() or path.is_symlink()


class _Archived(Exception):
    """Terminal journal won the serialization race; do not mutate any artifact."""


def invalidate(fence, error):
    try:
        with fence.transaction():
            if fence.state()["archive"]:
                raise _Archived
            fence.local_error = str(error)
            fence._append("ERROR", {"error": str(error)})
            latch(fence.directory, error)
    except _Archived:
        return
    except Exception:  # noqa: BLE001 - signed sink failure still needs durable denial
        try:
            # Failed signing/append rolled back. Reacquire the same writer lock
            # before fallback denial: ARCHIVE may have won in between.
            with fence.transaction():
                if fence.state()["archive"]:
                    raise _Archived
                fence.local_error = str(error)
                latch(fence.directory, error)
        except _Archived:
            return
        except Exception:  # noqa: BLE001 - damaged/unavailable journal cannot veto source work
            fence.local_error = str(error)
            latch(fence.directory, error)


def latch(directory, error):
    path = directory / "capture-failed.json"
    try:
        with path.open("xb") as stream:
            stream.write(json.dumps({"comparisonEligible": False,
                                     "error": type(error).__name__}).encode())
            stream.flush()
            os.fsync(stream.fileno())
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except FileExistsError:
        pass  # any existing bytes are already a denial, never authorization
    except Exception:  # noqa: BLE001 - failure diagnostics cannot veto source work
        LOGGER.error("capture denial latch storage unavailable; generation requires operator quarantine")


def retain_target(settings, fence):
    """Retain a public, verified denial target; never an authorization source."""
    value = {"authority": fence.authority, "pin": fence.authority_pin,
             "spool": str(fence.directory.resolve()),
             "authorityPath": str(Path(settings.shadow_capture_authority_path).resolve()),
             "keyPath": str(Path(settings.shadow_capture_private_key_path).resolve())}
    targets = ((fence.directory / "capture-custody.json", value),
               (Path(settings.shadow_capture_health_path).with_suffix(".custody.json"),
                {**value, "healthPath": str(Path(settings.shadow_capture_health_path).resolve())}))
    for path, target in targets:
        encoded = json.dumps(target, sort_keys=True).encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != encoded:
                raise ValueError("retained denial target changed")
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def invalidate_retained(settings, error):
    """Before Fence construction, deny only a matching previously verified spool.

    Bad inputs cannot redirect denial to another run/custody target. Verify the
    retained independent approval and actual signed ARM using public material;
    neither the failing pin nor a missing/bad private key is needed for denial.
    """
    if not settings.shadow_capture_fence_path and not settings.shadow_capture_health_path:
        return
    try:
        health_target = (Path(settings.shadow_capture_health_path).with_suffix(".custody.json")
                         if settings.shadow_capture_health_path else None)
        path = (health_target if health_target is not None and health_target.exists() else
                Path(settings.shadow_capture_fence_path).resolve() / "capture-custody.json")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                return
            target = json.load(stream)
        directory = Path(target["spool"])
        if settings.shadow_capture_fence_path:
            directory = Path(settings.shadow_capture_fence_path).resolve()
        elif str(Path(settings.shadow_capture_health_path).resolve()) != target.get("healthPath"):
            return
        if target["spool"] != str(directory):
            return
        matches = 0
        for configured, field in ((settings.shadow_capture_authority_path, "authorityPath"),
                                  (settings.shadow_capture_private_key_path, "keyPath")):
            if configured:
                matches += str(Path(configured).resolve()) == target[field]
        authority = target["authority"]
        binding = authority["binding"]
        health_matches = (bool(settings.shadow_capture_health_path) and
                          str(Path(settings.shadow_capture_health_path).resolve()) == target.get("healthPath"))
        if not matches and not health_matches:
            return  # no retained custody anchor: never guess from a path alone
        if "healthPath" in target and not health_matches:
            return
        for configured, field in ((settings.shadow_capture_run_id, "runId"),
                                  (settings.shadow_capture_generation_id, "generationId")):
            # A registered generation-specific health/custody target identifies
            # the intended spool even when the newly supplied identity is bad.
            if configured and configured != binding[field] and not health_matches:
                return
        from polycopy.shadow_capture import load_protocol
        a = load_protocol("production_authority.py")
        a.validate(authority, target["pin"], binding)
        database = directory / "source_fence.sqlite"
        if database.is_symlink():
            return
        db = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, isolation_level=None)
        try:
            # No journal mutation. Serialize denial with the protocol's ARCHIVE
            # lock so a terminal race cannot create a post-ARCHIVE sidecar.
            db.execute("BEGIN IMMEDIATE")
            arm = json.loads(db.execute("SELECT body FROM events WHERE seq=1").fetchone()[0])
            a.verify(arm, binding, authority, "fence")
            if arm["kind"] != "ARM" or arm["payload"] != binding:
                return
            # Terminal artifacts must remain untouched, even on a bad restart.
            if db.execute("SELECT 1 FROM events WHERE json_extract(body,'$.kind')='ARCHIVE'").fetchone():
                return
            latch(directory, error)
        finally:
            db.rollback()
            db.close()
    except Exception:  # noqa: BLE001 - no guessed denial target or source veto
        LOGGER.error("capture failure target unavailable; preserve failed installation for operator quarantine")

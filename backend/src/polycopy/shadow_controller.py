"""Explicit offline capture controls; never enrolls or acknowledges a worker.

Requires existing approved spool and installed identities. PostgreSQL operations
use a fresh read-only connection. No provider, trading or comparator calls.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import polycopy.shadow_capture as capture
from polycopy.config import Settings
from polycopy.shadow_failure import failed, invalidate
from polycopy.shadow_reader import read_operation


def open_fence(settings):
    if not settings.shadow_capture_enabled:
        raise ValueError("capture disabled")
    a = capture.load_protocol("production_authority.py")
    authority = json.loads(Path(settings.shadow_capture_authority_path).read_text())
    binding = authority["binding"]
    a.validate(authority, settings.shadow_capture_authority_pin, binding)
    capture.validate_installed_identity(binding, settings)
    roots = [Path(p).resolve() for p in (settings.shadow_capture_fence_path,
             settings.shadow_capture_drain_path, settings.shadow_capture_handoff_path) if p]
    if len(roots) != 3:
        raise ValueError("explicit evidence paths required")
    for p in (settings.shadow_capture_authority_path, settings.shadow_capture_private_key_path):
        if any(Path(p).resolve().is_relative_to(root) for root in roots):
            raise ValueError("custody input inside producer evidence root")
    if not (roots[0] / "source_fence.sqlite").is_file():
        raise ValueError("existing worker-armed spool required; controller cannot arm")
    if failed(roots[0]):
        raise ValueError("retained capture denial latch")
    key = a.load_private_key(settings.shadow_capture_private_key_path, binding)
    f = capture.load_protocol("poly2-comparison-fence.py")
    return f, f.Fence(roots[0], binding, authority=authority,
                     authority_pin=settings.shadow_capture_authority_pin, private_key=key)


async def operate(settings, command, *, database_url=None):
    f, fence = open_fence(settings)
    try:
        if command == "status":
            return fence.status()
        if command == "close":
            return fence.close_epoch()
        if command == "fence":
            return fence.complete()
        def read(reader, adapter):
            if command == "recover":
                fence.recover(reader)
                return fence.status()
            if command == "freeze":
                return fence.freeze(adapter)
            drain = f.FencedDrain(settings.shadow_capture_drain_path, fence, adapter,
                                  read_clock=fence.clock)
            try:
                result = drain.step()
                if command == "drain":
                    return result
                if command == "handoff":
                    # Exclusive directory publication; exact upstream file ABI.
                    return drain.write_seal_journals(settings.shadow_capture_handoff_path)
                raise ValueError("unknown controller command")
            finally:
                drain.close()
        try:
            return await read_operation(database_url or settings.database_url, read)
        except Exception as exc:
            invalidate(fence, exc)
            raise
    finally:
        fence.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "recover", "close", "fence", "freeze", "drain", "handoff"])
    args = parser.parse_args()
    result = asyncio.run(operate(Settings(), args.command))
    print(json.dumps(result, separators=(",", ":")))
    return 2 if result is None else 0


if __name__ == "__main__":
    raise SystemExit(main())

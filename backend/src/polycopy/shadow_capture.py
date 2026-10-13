"""Opt-in, fail-contained prospective capture. Never supplies its own authority.

The immutable reviewed protocol is vendored byte-for-byte. This module owns only
installation checks, lifetime identity custody and per-process epoch control.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import importlib.util
import json
import logging
import os
import subprocess
from pathlib import Path

from polycopy.shadow_failure import failed, invalidate, invalidate_retained, retain_target

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).parent
PROTOCOL = ROOT / "_shadow_protocol"
PROTOCOL_SOURCE_SHADOW_SHA = "fc8eb3b755c22695179c75968a84721be7590232"
_state = {"state": "DISABLED", "comparisonEligible": False}
_runtime = None


def load_protocol(name):
    path = PROTOCOL / name
    spec = importlib.util.spec_from_file_location("polycopy_reviewed_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def status():
    return dict(_state)


def identities():
    """Reproducible JCS maps of relative filename -> SHA256 of installed bytes."""
    authority = load_protocol("production_authority.py")
    manifest = json.loads((PROTOCOL / "manifest.json").read_text())
    actual = {name: hashlib.sha256((PROTOCOL / name).read_bytes()).hexdigest()
              for name in manifest["files"]}
    if actual != manifest["files"] or manifest["shadowSha"] != PROTOCOL_SOURCE_SHADOW_SHA:
        raise ValueError("installed reviewed protocol bytes differ")
    component = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                 for name in ("shadow_capture.py", "shadow_observer.py", "shadow_reader.py", "shadow_controller.py", "shadow_failure.py", "db.py")}
    return authority.digest(actual), authority.digest(component)


def deployed_sha():
    # A deployed source checkout is required. No supplied SHA is execution proof.
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=5
    ).strip()


def validate_installed_identity(binding, settings):
    """Check locally provable producer identities after independent authority validation.

    shadowCodeSha is required/validated by the pinned authority, not derived from
    protocol provenance. The Shadow launcher proves its checkout against that SHA.
    Both enrollment and lifecycle controls use this same producer comparison.
    """
    protocol, component = identities()
    expected = {"poly2CodeSha": deployed_sha(),
                "protocolDigest": protocol, "componentSha256": component,
                "runId": settings.shadow_capture_run_id,
                "generationId": settings.shadow_capture_generation_id}
    if any(binding.get(k) != v for k, v in expected.items()):
        raise ValueError("independently approved installed identity mismatch")


def _fail(settings, error):
    global _state
    # Do not emit exception text: crypto/config exceptions can contain input bytes.
    _state = {"state": "FAILED", "comparisonEligible": False,
              "error": type(error).__name__}
    LOGGER.error("shadow capture invalidated (%s); source availability unchanged",
                 type(error).__name__)
    if settings.shadow_capture_health_path:
        try:
            path = Path(settings.shadow_capture_health_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".pending")
            temp.write_text(json.dumps(_state))
            os.replace(temp, path)
        except Exception:  # noqa: BLE001, S110 - best-effort diagnostic, never scientific authority
            pass


class CaptureFactory:
    def __init__(self, maker, fence, worker, owner, settings):
        from polycopy.models import Market, Trade, Wallet
        from polycopy.shadow_observer import ObservedSessionFactory
        self.observed = ObservedSessionFactory(maker, fence, worker, Trade, Wallet, Market)
        self.maker, self.fence, self.worker = maker, fence, worker
        self.owner, self.settings = owner, settings
        info = os.stat(fence.directory / "source_fence.sqlite")
        self.spool_identity = info.st_dev, info.st_ino
        self.task = None
        self.retired = False
        try:
            self.task = asyncio.get_running_loop().create_task(self._control_loop())
        except RuntimeError:
            # Called synchronously before loop startup: first session starts control.
            pass

    def control(self):
        if self.retired:
            self.observed.retire()
            return
        try:
            info = os.stat(self.fence.directory / "source_fence.sqlite")
            if (info.st_dev, info.st_ino) != self.spool_identity:
                raise ValueError("retained spool replaced")
            state = self.fence.state()
            if self.fence.local_error or state["error"] or failed(self.fence.directory):
                raise ValueError("retained capture failure")
            if state["archive"]:
                self.retired = True
                self.observed.retire()
                return
            if state["closed"]:
                self.observed.acknowledge_epoch()
        except Exception as exc:  # noqa: BLE001 - observation must never veto source availability
            invalidate(self.fence, exc)
            _fail(self.settings, exc)
            self.retired = True  # fail-stop observation, not the source session factory
            self.observed.retire()

    async def _control_loop(self):
        while not self.retired:
            self.control()
            await asyncio.sleep(0.1)

    def __call__(self, **kwargs):
        self.control()
        if self.retired or self.fence.local_error:
            return self.maker(**kwargs)
        if self.task is None:
            try:
                self.task = asyncio.get_running_loop().create_task(self._control_loop())
            except RuntimeError:
                pass
        return self.observed(**kwargs)

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.retired = True
        self.observed.retire()
        self.fence.close()
        self.owner.close()


def install_factory(maker, settings):
    global _state, _runtime
    if not settings.shadow_capture_enabled:
        _state = {"state": "DISABLED", "comparisonEligible": False}
        return maker
    owner = None
    fence = None
    runtime = None
    try:
        required = (settings.shadow_capture_authority_path, settings.shadow_capture_authority_pin,
                    settings.shadow_capture_private_key_path, settings.shadow_capture_worker,
                    settings.shadow_capture_service, settings.shadow_capture_fence_path,
                    settings.shadow_capture_drain_path, settings.shadow_capture_handoff_path,
                    settings.shadow_capture_health_path, settings.shadow_capture_run_id,
                    settings.shadow_capture_generation_id)
        if not all(required):
            raise ValueError("explicit approved capture inputs required")
        worker = settings.shadow_capture_worker
        if settings.shadow_capture_service not in ("backend", "bot"):
            raise ValueError("non-writer service cannot enroll")
        if worker.split(":", 1)[0] != settings.shadow_capture_service:
            raise ValueError("worker/service mismatch")
        a = load_protocol("production_authority.py")
        authority = json.loads(Path(settings.shadow_capture_authority_path).read_text())
        binding = authority["binding"]
        a.validate(authority, settings.shadow_capture_authority_pin, binding)
        validate_installed_identity(binding, settings)
        if worker not in binding["expectedWorkers"]:
            raise ValueError("undeclared worker incarnation")
        roots = [Path(p).resolve() for p in (settings.shadow_capture_fence_path,
                 settings.shadow_capture_drain_path, settings.shadow_capture_handoff_path)]
        for input_path in (settings.shadow_capture_authority_path,
                           settings.shadow_capture_private_key_path):
            if any(Path(input_path).resolve().is_relative_to(root) for root in roots):
                raise ValueError("custody input inside producer evidence root")
        key = a.load_private_key(settings.shadow_capture_private_key_path, binding)
        spool = roots[0]
        spool.mkdir(parents=True, exist_ok=True)
        if failed(spool):
            raise ValueError("retained denial latch; generation cannot reopen")
        f = load_protocol("poly2-comparison-fence.py")
        fence = f.Fence(spool, binding, authority=authority,
                        authority_pin=settings.shadow_capture_authority_pin, private_key=key)
        retain_target(settings, fence)
        # Open the verified journal first so duplicate ownership is durably latched
        # for the already-running process as well, not only its contender's cache.
        owner = open(spool / (worker.replace(":", "-") + ".owner.lock"), "a+b")  # noqa: SIM115 - lifetime handle
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runtime = CaptureFactory(maker, fence, worker, owner, settings)
        if fence.local_error or fence.state()["error"]:
            raise ValueError("capture enrollment failed")
        _state = {"state": "ACTIVE", "comparisonEligible": False, "worker": worker}
        _runtime = runtime
        return runtime
    except Exception as exc:  # noqa: BLE001 - observation must never veto source availability
        if runtime is not None and runtime.task is not None:
            runtime.task.cancel()
        if fence is not None:
            invalidate(fence, exc)
            fence.close()
        else:
            invalidate_retained(settings, exc)
        if owner is not None:
            owner.close()
        _fail(settings, exc)
        return maker


async def shutdown():
    global _runtime
    if _runtime is not None:
        await _runtime.close()
        _runtime = None

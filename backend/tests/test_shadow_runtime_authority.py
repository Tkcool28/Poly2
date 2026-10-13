"""Independent authority runtime revision is distinct from frozen protocol provenance.

Disposable keys/authority/spools only; real validation, enrollment and controller
reopen, without PostgreSQL or any provider/trading access.
"""
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import polycopy.shadow_capture as capture
from polycopy.config import Settings
from polycopy.shadow_controller import open_fence

SHADOW_RUNTIME = "71ddacf318cccb1c5bef1a2263ac6cb00ae75ed3"


@pytest.fixture
async def approval(tmp_path, monkeypatch):
    a = capture.load_protocol("production_authority.py")
    f = capture.load_protocol("poly2-comparison-fence.py")
    clock = "2026-10-12T00:00:00Z"
    original_fence = f.Fence
    monkeypatch.setattr(f, "Fence", lambda *args, **kw: original_fence(
        *args, read_clock=lambda: clock, **kw))
    real_load = capture.load_protocol
    monkeypatch.setattr(capture, "load_protocol", lambda name: f if name ==
                        "poly2-comparison-fence.py" else real_load(name))
    key = Ed25519PrivateKey.generate()
    protocol, component = capture.identities()
    binding = {"runId": "disposable", "shadowRunId": "disposable-shadow",
                   "generationId": "disposable-generation", "poly2CodeSha": capture.deployed_sha(),
                   "shadowCodeSha": SHADOW_RUNTIME, "protocolDigest": protocol, "componentSha256": component,
                   "cohort": ["0x" + str(i) * 40 for i in range(1, 6)],
                   "window": {"startUtc": "2026-10-13T00:00:00Z", "endUtc": "2026-10-14T00:00:00Z"},
                   "evidenceKind": "observational", "expectedWorkers": ["backend:a", "bot:a"],
                   "sourcePublicKey": a.public_pem(key.public_key())}
    authority = {"schemaVersion": 1, "version": "poly2-production-authority-v1", "binding": binding,
                     "cohortSha256": a.digest(binding["cohort"]), "requiredWriterServices": ["backend", "bot"],
                     "producerFingerprint": a.fingerprint(binding["sourcePublicKey"]), "approvedUtc": clock,
                     "installations": {w: {"installationSha256": "5" * 64, "approvedUtc": clock}
                                    for w in binding["expectedWorkers"]}}
    keyfile = tmp_path / "key.pem"
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                       serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    keyfile.chmod(0o600)
    path = tmp_path / "authority.json"
    path.write_text(a.canonical(authority))
    settings = Settings(_env_file=None, shadow_capture_enabled=True,
                        shadow_capture_service="backend", shadow_capture_worker="backend:a",
                        shadow_capture_authority_path=str(path), shadow_capture_authority_pin=a.digest(authority),
                        shadow_capture_private_key_path=str(keyfile), shadow_capture_run_id=binding["runId"],
                        shadow_capture_generation_id=binding["generationId"],
                        shadow_capture_fence_path=str(tmp_path / "fence"),
                        shadow_capture_drain_path=str(tmp_path / "drain"),
                        shadow_capture_handoff_path=str(tmp_path / "handoff"),
                        shadow_capture_health_path=str(tmp_path / "health.json"))
    yield a, authority, settings
    await capture.shutdown()


async def test_runtime_shadow_revision_not_rejected_by_vendored_protocol_provenance(approval):
    a, authority, settings = approval
    manifest = json.loads((capture.PROTOCOL / "manifest.json").read_text())
    assert authority["binding"]["shadowCodeSha"] == SHADOW_RUNTIME
    assert manifest["shadowSha"] == capture.PROTOCOL_SOURCE_SHADOW_SHA
    assert SHADOW_RUNTIME != manifest["shadowSha"]
    maker = object()
    runtime = capture.install_factory(maker, settings)
    assert isinstance(runtime, capture.CaptureFactory), capture.status()
    _, fence = open_fence(settings)
    try:
        assert fence.binding == authority["binding"]
    finally:
        fence.close()
    assert a.validate(authority, settings.shadow_capture_authority_pin, authority["binding"])


@pytest.mark.parametrize("entry", ["install", "controller"])
@pytest.mark.parametrize("field", ["poly2CodeSha", "protocolDigest", "componentSha256", "runId", "generationId"])
async def test_local_identity_mismatch_fails_both_paths(approval, entry, field):
    a, authority, settings = approval
    authority["binding"][field] = "0" * (40 if field == "poly2CodeSha" else 64)
    Path(settings.shadow_capture_authority_path).write_text(a.canonical(authority))
    settings = settings.model_copy(update={"shadow_capture_authority_pin": a.digest(authority)})
    assert a.validate(authority, settings.shadow_capture_authority_pin, authority["binding"])
    assert_rejected(entry, settings, "independently approved installed identity mismatch")


def assert_rejected(entry, settings, message=None):
    before = settings.model_dump()
    if entry == "install":
        maker = object()
        assert capture.install_factory(maker, settings) is maker
        assert capture.status()["state"] == "FAILED"
    else:
        with pytest.raises((ValueError, TypeError), match=message):
            open_fence(settings)
    assert settings.model_dump() == before
    assert settings.paper_mode and not settings.allow_live_trading and settings.order_kill_switch
    assert settings.max_exposure_global_usd == 200 and settings.max_order_size_usd == 10


@pytest.mark.parametrize("entry", ["install", "controller"])
async def test_wrong_independent_pin_fails_both_paths(approval, entry):
    _, _, settings = approval
    assert_rejected(entry, settings.model_copy(update={"shadow_capture_authority_pin": "0" * 64}),
                    "independent authority pin mismatch")


@pytest.mark.parametrize("entry", ["install", "controller"])
@pytest.mark.parametrize("revision", [None, "", "1" * 39, "A" * 40, "g" * 40, 71])
async def test_shadow_revision_required_full_lowercase_hex(approval, entry, revision):
    a, authority, settings = approval
    if revision is None:
        del authority["binding"]["shadowCodeSha"]
    else:
        authority["binding"]["shadowCodeSha"] = revision
    Path(settings.shadow_capture_authority_path).write_text(a.canonical(authority))
    settings = settings.model_copy(update={"shadow_capture_authority_pin": a.digest(authority)})
    with pytest.raises((ValueError, TypeError)):
        a.validate(authority, settings.shadow_capture_authority_pin, authority["binding"])
    assert_rejected(entry, settings)


@pytest.mark.parametrize("entry", ["install", "controller"])
async def test_producer_recomputed_pin_cannot_replace_independent_pin(approval, entry):
    a, authority, settings = approval
    authority["binding"]["shadowCodeSha"] = "2" * 40
    producer_pin = a.digest(authority)
    assert producer_pin != settings.shadow_capture_authority_pin
    Path(settings.shadow_capture_authority_path).write_text(a.canonical(authority))
    # Valid shape/new producer hash cannot authorize against the unchanged external pin.
    assert a.validate(authority, producer_pin, authority["binding"])
    assert_rejected(entry, settings, "independent authority pin mismatch")


@pytest.mark.parametrize("entry", ["install", "controller"])
async def test_exact_vendored_protocol_bytes_remain_enforced(approval, tmp_path, monkeypatch, entry):
    _, _, settings = approval
    import shutil
    changed = tmp_path / "changed-protocol"
    shutil.copytree(capture.PROTOCOL, changed)
    with (changed / "bounded_journal.py").open("ab") as stream:
        stream.write(b"\n# changed bytes\n")
    monkeypatch.setattr(capture, "PROTOCOL", changed)
    with pytest.raises(ValueError, match="installed reviewed protocol bytes differ"):
        capture.identities()
    assert_rejected(entry, settings, "installed reviewed protocol bytes differ")


async def test_unapproved_worker_remains_fail_contained(approval):
    _, _, settings = approval
    assert_rejected("install", settings.model_copy(update={"shadow_capture_worker": "backend:unapproved"}))

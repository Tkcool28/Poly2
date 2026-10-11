"""Disposable native proofs: no implicit DSN, network/provider or live key.

Set POLY2_CAPTURE_TEST_SOCKET to an explicitly owned fixture Unix socket dir.
"""
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import polycopy.shadow_capture as capture
from polycopy.config import Settings
from polycopy.models import Base, Market, Signal, Trade, Wallet
from polycopy.shadow_reader import read_operation


@pytest.fixture
async def native(tmp_path, monkeypatch):
    socket = os.environ.get("POLY2_CAPTURE_TEST_SOCKET")
    if not socket:
        pytest.skip("explicit owned Unix socket fixture required")
    assert socket.startswith("/proc/") and socket.endswith("/root/var/run/postgresql")
    url = "postgresql+asyncpg://postgres@/postgres?host=" + socket
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        wallet = Wallet(address="0x" + "1" * 40)
        market = Market(condition_id="condition", question="Disposable")
        session.add_all([wallet, market])
        await session.commit()
    now = datetime.now(UTC)
    start = now - timedelta(hours=1)
    end = start + timedelta(days=1)
    clock = [(start - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")]
    a = capture.load_protocol("production_authority.py")
    f = capture.load_protocol("poly2-comparison-fence.py")
    monkeypatch.setattr(f.C, "now", lambda: clock[0])
    # Fence default argument is bound at import; inject at construction only.
    original_fence = f.Fence
    def fence(*args, **kwargs):
        return original_fence(*args, read_clock=lambda: clock[0], **kwargs)
    f.Fence = fence
    real_load = capture.load_protocol
    monkeypatch.setattr(capture, "load_protocol",
                        lambda name: f if name == "poly2-comparison-fence.py" else real_load(name))
    key = Ed25519PrivateKey.generate()
    protocol, component = capture.identities()
    binding = {"runId": "disposable-poly2", "shadowRunId": "disposable-shadow",
                   "generationId": "generation-v1", "poly2CodeSha": capture.deployed_sha(),
                   "shadowCodeSha": capture.SHADOW_SHA, "protocolDigest": protocol,
                   "componentSha256": component, "cohort": ["0x" + str(i) * 40 for i in range(1, 6)],
                   "window": {"startUtc": start.isoformat().replace("+00:00", "Z"),
                               "endUtc": end.isoformat().replace("+00:00", "Z")},
                   "evidenceKind": "observational", "expectedWorkers": ["backend:a", "bot:a"],
                   "sourcePublicKey": a.public_pem(key.public_key())}
    authority = {"schemaVersion": 1, "version": "poly2-production-authority-v1", "binding": binding,
                     "cohortSha256": a.digest(binding["cohort"]),
                     "requiredWriterServices": ["backend", "bot"],
                     "producerFingerprint": a.fingerprint(binding["sourcePublicKey"]),
                     "approvedUtc": clock[0], "installations": {w: {"installationSha256": "5" * 64,
                     "approvedUtc": clock[0]} for w in binding["expectedWorkers"]}}
    custody = tmp_path / "operator"
    custody.mkdir()
    keyfile = custody / "key.pem"
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                       serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    keyfile.chmod(0o600)
    authorityfile = custody / "authority.json"
    authorityfile.write_text(a.canonical(authority))
    settings = Settings(_env_file=None, shadow_capture_enabled=True,
                        shadow_capture_service="backend", shadow_capture_worker="backend:a",
                        shadow_capture_authority_path=str(authorityfile),
                        shadow_capture_authority_pin=a.digest(authority),
                        shadow_capture_private_key_path=str(keyfile),
                        shadow_capture_run_id=binding["runId"],
                        shadow_capture_generation_id=binding["generationId"],
                        shadow_capture_fence_path=str(tmp_path / "fence"),
                        shadow_capture_drain_path=str(tmp_path / "drain"),
                        shadow_capture_handoff_path=str(tmp_path / "handoff"),
                        shadow_capture_health_path=str(tmp_path / "backend-a-health.json"))
    workers = []
    def install(worker):
        cfg = settings.model_copy(update={"shadow_capture_worker": worker,
                                         "shadow_capture_service": worker.split(":")[0],
                                         "shadow_capture_health_path": str(tmp_path / (worker.replace(":", "-") + "-health.json"))})
        factory = capture.install_factory(maker, cfg)
        assert isinstance(factory, capture.CaptureFactory), capture.status()
        workers.append(factory)
        return factory
    backend, bot = install("backend:a"), install("bot:a")
    clock[0] = (start + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    yield {"url": url, "engine": engine, "maker": maker, "backend": backend, "bot": bot,
               "wallet": wallet, "market": market, "binding": binding, "authority": authority,
               "settings": settings, "clock": clock, "start": start, "end": end, "install": install, "a": a, "f": f}
    for worker in workers:
        await worker.close()
    capture._runtime = None
    await engine.dispose()


def make_trade(n, event="event:1", size="100.000001", at=None):
    return Trade(polymarket_trade_id=event, wallet_id=n["wallet"].id,
                 market_id=n["market"].id, asset_id="7", side="BUY", outcome="Yes",
                 price="0.5", size=size, traded_at=at or n["start"],
                 ingested_at=at or n["start"])


async def recover(n):
    await read_operation(n["url"], lambda reader, adapter: n["bot"].fence.recover(reader))


async def close_freeze(n):
    n["clock"][0] = (n["end"] + timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
    fence = n["bot"].fence
    fence.close_epoch()
    n["backend"].control()
    n["bot"].control()
    assert fence.complete()
    return await read_operation(n["url"], lambda reader, adapter: fence.freeze(adapter))


async def test_capture_native_atomicity(native):
    n = native
    async with n["bot"]() as session:
        session.add(make_trade(n))
        await session.commit()
    async with n["maker"]() as session:
        assert await session.scalar(text("SELECT count(*) FROM comparison_transaction_witness")) == 1
    assert n["bot"].fence.status()["outstanding"] == 1
    await recover(n)
    assert n["bot"].fence.status()["committed"] == 1
    await close_freeze(n)


@pytest.mark.parametrize("size,expected", [("10", "10.000000"), ("10.1200", "10.120000"),
                                           ("10.1234567", "10.123457")])
async def test_capture_native_precision(native, size, expected):
    n = native
    async with n["bot"]() as session:
        trade = make_trade(n, size=size, at=n["start"].astimezone(
            __import__("datetime").timezone(timedelta(hours=5, minutes=30))))
        session.add(trade)
        await session.commit()
    await recover(n)
    population = await close_freeze(n)
    assert population["payload"]["sourceFacts"][0]["originalSize"] == expected


async def test_capture_native_outer_nested_rollback(native):
    n = native
    async with n["bot"]() as session:
        outer = await session.begin_nested()
        child = await session.begin_nested()
        session.add(make_trade(n))
        await session.flush()
        await child.commit()
        await outer.rollback()
        session.add(make_trade(n, event="event:2"))
        await session.commit()
    await recover(n)
    p = await close_freeze(n)
    assert [t["sourceEventId"] for t in p["payload"]["sourceFacts"]] == ["event:2"]


async def test_capture_native_source_rollback(native):
    n = native
    async with n["bot"]() as session:
        session.add(make_trade(n))
        await session.flush()
        await session.rollback()
    async with n["maker"]() as session:
        assert await session.scalar(text("SELECT count(*) FROM trades")) == 0
        assert await session.scalar(text("SELECT count(*) FROM comparison_transaction_witness")) == 0
    assert n["bot"].fence.status()["rolledBack"] == 1


async def test_capture_native_witness_failure_contained(native):
    n = native
    async with n["engine"].begin() as conn:
        await conn.execute(text("DROP TABLE comparison_transaction_witness"))
    async with n["bot"]() as session:
        session.add(make_trade(n))
        await session.commit()
    async with n["maker"]() as session:
        assert await session.scalar(text("SELECT count(*) FROM trades")) == 1
    assert n["bot"].fence.status()["state"] == "FAILED"
    with pytest.raises(ValueError):
        n["bot"].fence.complete()


async def test_capture_native_initial_decision_audit(native):
    from polycopy.execution.service import _record_skip
    n = native
    async with n["bot"]() as session:
        trade = make_trade(n)
        session.add(trade)
        await session.commit()
        signal = Signal(wallet_id=trade.wallet_id, market_id=trade.market_id,
                        source_trade_id=trade.polymarket_trade_id, asset_id="7",
                        side="BUY", outcome="Yes", source_price="0.5",
                        t0_traded_at=trade.traded_at, t1_detected_at=trade.ingested_at)
        session.add(signal)
        await session.commit()
        signal_id = signal.id
    await recover(n)
    await close_freeze(n)
    async def step():
        def op(reader, adapter):
            d = n["f"].FencedDrain(n["settings"].shadow_capture_drain_path,
                                   n["bot"].fence, adapter, read_clock=lambda: n["clock"][0])
            try:
                return d.step()
            finally:
                d.close()
        return await read_operation(n["url"], op)
    assert (await step())["counts"]["PENDING"] == 1
    async with n["maker"]() as session:
        signal = await session.get(Signal, signal_id)
        await _record_skip(session, signal, reason="stale_signal",
                           now=n["end"] + timedelta(days=1), order_kwargs={})
        await session.rollback()
    assert (await step())["counts"]["PENDING"] == 1
    async with n["maker"]() as session:
        signal = await session.get(Signal, signal_id)
        await _record_skip(session, signal, reason="stale_signal",
                           now=n["end"] + timedelta(days=1), order_kwargs={})
        await session.commit()
    result = await step()
    assert result["comparisonEligible"]
    assert result["perId"]["event:1"]["decision"]["sourceAudit"]["action"] == "signal_skipped"


async def test_capture_duplicate_identity_latches_authority(native):
    n = native
    assert capture.install_factory(n["maker"], n["settings"]) is n["maker"]
    assert capture.status()["state"] == "FAILED"
    assert n["backend"].fence.state()["error"] is not None


async def test_capture_replaced_spool_restart_refuses(native):
    n = native
    backend = n["backend"]
    path = backend.fence.directory / "source_fence.sqlite"
    path.rename(path.with_suffix(".retained"))
    backend.control()
    assert backend.fence.local_error


@pytest.mark.parametrize("field,value", [
    ("shadow_capture_authority_pin", "0" * 64),
    ("shadow_capture_worker", "backend:undeclared"),
    ("shadow_capture_generation_id", "wrong"),
    ("shadow_capture_run_id", "wrong"),
    ("shadow_capture_service", "watchdog"),
    ("shadow_capture_authority_path", ""),
])
async def test_capture_config_failure_source_available(native, field, value):
    n = native
    settings = n["settings"].model_copy(update={field: value})
    assert capture.install_factory(n["maker"], settings) is n["maker"]
    assert capture.status()["state"] == "FAILED"
    assert settings.paper_mode and not settings.allow_live_trading and settings.order_kill_switch


@pytest.mark.parametrize("field", ["poly2CodeSha", "shadowCodeSha", "componentSha256", "protocolDigest"])
async def test_capture_actual_installed_identity(native, field):
    n = native
    path = Path(n["settings"].shadow_capture_authority_path)
    authority = json.loads(path.read_text())
    authority["binding"][field] = "0" * len(authority["binding"][field])
    path.write_text(n["a"].canonical(authority))
    settings = n["settings"].model_copy(update={"shadow_capture_authority_pin": n["a"].digest(authority)})
    assert capture.install_factory(n["maker"], settings) is n["maker"]
    assert capture.status()["state"] == "FAILED"


@pytest.mark.parametrize("mode", [0o644, 0o400, 0o700])
async def test_capture_key_mode(native, mode):
    n = native
    Path(n["settings"].shadow_capture_private_key_path).chmod(mode)
    assert capture.install_factory(n["maker"], n["settings"]) is n["maker"]


async def test_capture_key_symlink(native):
    n = native
    path = Path(n["settings"].shadow_capture_private_key_path)
    path.rename(path.with_suffix(".real"))
    path.symlink_to(path.with_suffix(".real"))
    assert capture.install_factory(n["maker"], n["settings"]) is n["maker"]


async def test_capture_native_unknown_not_guessed(native):
    n = native
    token = n["bot"].fence._admit("bot:a")
    await recover(n)
    assert n["bot"].fence.state()["attempts"][token]["state"] == "OUTSTANDING"
    n["clock"][0] = (n["end"] + timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
    n["bot"].fence.close_epoch()
    n["bot"].control()
    n["backend"].control()
    assert n["bot"].fence.complete() is None


async def test_capture_ack_idempotence_conflict_missing(native):
    n = native
    n["clock"][0] = (n["end"] + timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
    f = n["bot"].fence
    f.close_epoch()
    n["bot"].control()
    assert f.complete() is None
    count = len(f.events())
    n["bot"].control()
    assert len(f.events()) == count
    with pytest.raises(ValueError), f.transaction():
        f._append("ACK", {"worker": "bot:a", "epoch": 0})
    n["backend"].control()
    assert f.complete()


async def test_capture_close_exact_inclusive_endpoint(native):
    n = native
    n["clock"][0] = n["binding"]["window"]["endUtc"]
    with pytest.raises(ValueError):
        n["bot"].fence.close_epoch()
    n["clock"][0] = (n["end"] + timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
    assert n["bot"].fence.close_epoch()


async def test_capture_native_reader_denies_write(native):
    n = native
    from sqlalchemy.exc import DBAPIError
    def op(reader, adapter):
        with pytest.raises(DBAPIError):
            reader.cursor().execute("INSERT INTO wallets(address) VALUES('forbidden')")
        reader.rollback()
        return reader.cursor().execute("SHOW default_transaction_read_only").fetchall()
    assert await read_operation(n["url"], op) == [("on",)]


async def test_capture_native_official_cli(native, tmp_path):
    import subprocess

    from sqlalchemy import select

    from polycopy.execution.service import execute_signal
    from polycopy.ingestion.service import ingest_trade_rows
    n = native
    shadow = os.environ.get("POLY2_CAPTURE_SHADOW_ROOT")
    if not shadow:
        pytest.skip("explicit exact disposable Shadow checkout required")
    class NoNetwork:
        def __getattr__(self, name):
            raise AssertionError("provider access forbidden: " + name)
    raw = {"transactionHash": "0x" + "a" * 64, "proxyWallet": n["wallet"].address,
           "asset": "7", "conditionId": "condition", "side": "BUY", "outcome": "Yes",
           "size": "100.000001", "price": "0.5", "timestamp": int(n["start"].timestamp())}
    async with n["bot"]() as session:
        wallet = await session.get(Wallet, n["wallet"].id)
        result = await ingest_trade_rows(session, NoNetwork(), wallet, [raw],
                                         fetch_market_metadata=False)
        assert result.inserted == 1 and result.quarantined == 0
        trade = (await session.execute(select(Trade))).scalar_one()
        signal = Signal(wallet_id=trade.wallet_id, market_id=trade.market_id,
                        source_trade_id=trade.polymarket_trade_id, asset_id="7", side="BUY",
                        outcome="Yes", source_price="0.5", t0_traded_at=trade.traded_at,
                        t1_detected_at=trade.ingested_at)
        session.add(signal)
        await session.commit()
        signal_id = signal.id
    # Reopen actual producer without any post-commit callback resolution.
    bot = n["bot"]
    await bot.close()
    n["install"]("bot:a")
    # The fixture cleanup owns closed handles too; remove the closed object.
    bot.close = _noop_close
    restarted = capture._runtime
    n["bot"] = restarted
    await recover(n)
    assert restarted.fence.status()["committed"] == 1
    await close_freeze(n)
    async with n["maker"]() as session:
        signal = await session.get(Signal, signal_id)
        order = await execute_signal(session, NoNetwork(), signal,
                                     now=n["end"] + timedelta(days=1))
        assert order.miss_reason == "stale_signal"
    n["clock"][0] = (n["end"] + timedelta(days=1, seconds=1)).isoformat().replace("+00:00", "Z")
    from polycopy.shadow_controller import operate
    assert (await operate(n["settings"], "drain", database_url=n["url"]))["comparisonEligible"]
    await operate(n["settings"], "handoff", database_url=n["url"])
    handoff = Path(n["settings"].shadow_capture_handoff_path)
    (handoff / "authority.json").write_text(n["a"].canonical(n["authority"]))
    (handoff / "pin").write_text(n["a"].digest(n["authority"]))
    script = Path(__file__).with_name("shadow_capture_e2e.mts")
    import asyncio
    result = await asyncio.to_thread(subprocess.run, ["node", "--import", "tsx", str(script), shadow, str(handoff)],
                            cwd=shadow, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    print("OFFICIAL_POLY2_NATIVE " + result.stdout.strip())
    before = len(n["bot"].fence.events())
    n["bot"].control()
    assert n["bot"].retired
    async with n["bot"]() as session:
        session.add(make_trade(n, event="event:after-archive", at=n["end"] + timedelta(seconds=2)))
        await session.commit()
    assert len(n["bot"].fence.events()) == before


@pytest.mark.parametrize("seam", ["wallets", "markets", "trades"])
async def test_capture_observation_read_sql_failure_preserves_source(native, seam):
    from sqlalchemy import event
    n = native
    hit = []
    def inject(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT ") and ("FROM " + seam) in statement:
            hit.append(statement)
            return "SELECT 1/0", ()
        return statement, parameters
    event.listen(n["engine"].sync_engine, "before_cursor_execute", inject, retval=True)
    try:
        async with n["bot"]() as session:
            session.add(make_trade(n))
            await session.commit()
    finally:
        event.remove(n["engine"].sync_engine, "before_cursor_execute", inject)
    assert len(hit) == 1
    async with n["maker"]() as session:
        assert await session.scalar(text("SELECT count(*) FROM trades")) == 1
    assert n["bot"].fence.status()["state"] == "FAILED"


@pytest.mark.parametrize("applied", [True, False])
async def test_capture_commit_response_failure_cleanup_is_not_rollback_proof(native, monkeypatch, applied):
    n = native
    dialect = n["engine"].sync_engine.dialect
    original = dialect.do_commit
    calls = []
    def uncertain(connection):
        calls.append(1)
        if applied:
            original(connection)
        raise RuntimeError("disposable uncertain COMMIT response")
    async with n["bot"]() as session:
        session.add(make_trade(n))
        with monkeypatch.context() as patch:
            patch.setattr(dialect, "do_commit", uncertain)
            with pytest.raises(RuntimeError, match="uncertain COMMIT"):
                await session.commit()
        await session.rollback()
    assert calls == [1]
    async with n["maker"]() as session:
        assert await session.scalar(text("SELECT count(*) FROM trades")) == int(applied)
        assert await session.scalar(text("SELECT count(*) FROM comparison_transaction_witness")) == int(applied)
    assert n["bot"].fence.status()["rolledBack"] == 0
    assert n["bot"].fence.status()["outstanding"] == 1
    await recover(n)
    assert n["bot"].fence.status()["committed"] == int(applied)
    if applied:
        assert (await close_freeze(n))["payload"]["count"] == 1
    else:
        n["clock"][0] = (n["end"] + timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
        n["bot"].fence.close_epoch()
        n["bot"].control()
        n["backend"].control()
        assert n["bot"].fence.complete() is None


@pytest.mark.parametrize("field,value", [
    ("shadow_capture_authority_pin", "0" * 64),
    ("shadow_capture_authority_path", ""),
    ("shadow_capture_private_key_path", ""),
    ("shadow_capture_worker", "backend:wrong"),
    ("shadow_capture_fence_path", ""),
    ("shadow_capture_run_id", "wrong"),
    ("shadow_capture_generation_id", "wrong"),
    ("shadow_capture_private_key_path", "/nonexistent-disposable-key"),
    ("shadow_capture_authority_path", "/nonexistent-disposable-authority"),
    ("shadow_capture_authority_pin", ""),
    ("shadow_capture_worker", ""),
    ("shadow_capture_service", ""),
    ("shadow_capture_drain_path", ""),
    ("shadow_capture_handoff_path", ""),
    ("shadow_capture_health_path", ""),
    ("shadow_capture_run_id", ""),
    ("shadow_capture_generation_id", ""),
])
async def test_capture_pre_fence_failure_correction_cannot_certify(native, field, value):
    from polycopy.shadow_controller import operate
    n = native
    await n["backend"].close()
    n["backend"].close = _noop_close
    bad = n["settings"].model_copy(update={field: value})
    assert capture.install_factory(n["maker"], bad) is n["maker"]
    assert (Path(n["settings"].shadow_capture_fence_path) / "capture-failed.json").is_file()
    assert capture.install_factory(n["maker"], n["settings"]) is n["maker"]
    for command in ("fence", "freeze", "handoff"):
        with pytest.raises(ValueError, match="denial latch"):
            await operate(n["settings"], command, database_url=n["url"])
    async with n["maker"]() as session:
        session.add(make_trade(n))
        await session.commit()
    async with n["maker"]() as session:
        assert await session.scalar(text("SELECT count(*) FROM trades")) == 1


@pytest.mark.parametrize("control_first", [False, True])
async def test_capture_archive_retires_existing_idle_session(native, control_first):
    from polycopy.shadow_controller import operate
    n = native
    async with n["bot"]() as old:
        observer = old.sync_session.info["poly2_comparison_observer"]
        await close_freeze(n)
        await operate(n["settings"], "drain", database_url=n["url"])
        await operate(n["settings"], "handoff", database_url=n["url"])
        directory = n["bot"].fence.directory
        before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
        if control_first:
            n["bot"].control()
            assert observer.listeners == []
        old.add(make_trade(n, event="old:after-archive"))
        await old.commit()
        async with n["bot"]() as fresh:
            fresh.add(make_trade(n, event="new:after-archive"))
            await fresh.commit()
        assert not n["bot"].fence.local_error
        assert not (directory / "capture-failed.json").exists()
        assert {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()} == before
    async with n["maker"]() as session:
        assert await session.scalar(text("SELECT count(*) FROM trades")) == 2


async def _noop_close():
    pass


async def test_capture_commit_then_new_transaction_real_rollback(native):
    n = native
    async with n["bot"]() as session:
        session.add(make_trade(n))
        await session.commit()
        session.add(make_trade(n, event="rollback:second"))
        await session.flush()
        await session.rollback()
    assert n["bot"].fence.status()["rolledBack"] == 1
    await recover(n)
    assert (await close_freeze(n))["payload"]["count"] == 1


async def test_capture_prefence_existing_session_blocks_archive(native):
    from polycopy.shadow_controller import operate
    n = native
    async with n["bot"]() as session:
        session.add(make_trade(n))
        await session.flush()
        n["clock"][0] = (n["end"] + timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
        n["bot"].fence.close_epoch()
        n["backend"].control()
        n["bot"].control()
        assert n["bot"].fence.complete() is None
        with pytest.raises(ValueError):
            await operate(n["settings"], "handoff", database_url=n["url"])
        assert not n["bot"].fence.state()["archive"]
        # The controller failure invalidates comparison but not the real write.
        await session.commit()
    async with n["maker"]() as session:
        assert await session.scalar(text("SELECT count(*) FROM trades")) == 1


async def test_capture_archive_races_existing_session_admission(native, monkeypatch):
    n = native
    async with n["bot"]() as old:
        await close_freeze(n)
        def drain(reader, adapter):
            d = n["f"].FencedDrain(n["settings"].shadow_capture_drain_path,
                n["bot"].fence, adapter, read_clock=lambda: n["clock"][0])
            try:
                return d.step()
            finally:
                d.close()
        frame = await read_operation(n["url"], drain)
        fence = n["bot"].fence
        original = fence.state
        snapshot = []
        def archive_between_check_and_admission():
            state = original()
            if not snapshot:
                state = dict(state)
                n["backend"].fence.authenticate_archive(frame)
                snapshot.append({p.name: p.read_bytes() for p in fence.directory.iterdir() if p.is_file()})
            return state
        monkeypatch.setattr(fence, "state", archive_between_check_and_admission)
        old.add(make_trade(n, event="race:after-archive"))
        await old.commit()
        assert not fence.local_error
        assert snapshot
        assert {p.name: p.read_bytes() for p in fence.directory.iterdir() if p.is_file()} == snapshot[0]
    async with n["maker"]() as session:
        assert await session.scalar(text("SELECT count(*) FROM trades")) == 1


async def test_capture_observation_failure_racing_archive_cannot_mutate_terminal(native):
    from sqlalchemy import event
    n = native
    async with n["bot"]() as old:
        await close_freeze(n)
        def drain(reader, adapter):
            d = n["f"].FencedDrain(n["settings"].shadow_capture_drain_path,
                n["bot"].fence, adapter, read_clock=lambda: n["clock"][0])
            try:
                return d.step()
            finally:
                d.close()
        frame = await read_operation(n["url"], drain)
        snapshot = []
        def archive_then_sql_error(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("SELECT ") and "FROM wallets" in statement:
                n["backend"].fence.authenticate_archive(frame)
                snapshot.append({p.name: p.read_bytes() for p in n["bot"].fence.directory.iterdir() if p.is_file()})
                return "SELECT 1/0", ()
            return statement, parameters
        event.listen(n["engine"].sync_engine, "before_cursor_execute", archive_then_sql_error, retval=True)
        try:
            old.add(make_trade(n, event="read-failure:after-close", at=n["end"] + timedelta(seconds=2)))
            await old.commit()
        finally:
            event.remove(n["engine"].sync_engine, "before_cursor_execute", archive_then_sql_error)
        assert snapshot
        assert not n["bot"].fence.local_error
        assert {p.name: p.read_bytes() for p in n["bot"].fence.directory.iterdir() if p.is_file()} == snapshot[0]
    async with n["maker"]() as session:
        assert await session.scalar(text("SELECT count(*) FROM trades")) == 1


async def test_capture_archive_control_during_source_commit_dispatch(native):
    from sqlalchemy import event
    n = native
    async with n["bot"]() as old:
        await close_freeze(n)
        def drain(reader, adapter):
            d = n["f"].FencedDrain(n["settings"].shadow_capture_drain_path,
                n["bot"].fence, adapter, read_clock=lambda: n["clock"][0])
            try:
                return d.step()
            finally:
                d.close()
        frame = await read_operation(n["url"], drain)
        observer = old.sync_session.info["poly2_comparison_observer"]
        snapshot = []
        def control_during_flush(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO trades"):
                assert observer.dispatching > 0
                n["backend"].fence.authenticate_archive(frame)
                n["bot"].control()
                assert observer.retired and observer.listeners
                snapshot.append({p.name: p.read_bytes() for p in n["bot"].fence.directory.iterdir() if p.is_file()})
        event.listen(n["engine"].sync_engine, "before_cursor_execute", control_during_flush)
        try:
            old.add(make_trade(n, event="control:after-close", at=n["end"] + timedelta(seconds=2)))
            await old.commit()
        finally:
            event.remove(n["engine"].sync_engine, "before_cursor_execute", control_during_flush)
        n["bot"].control()
        assert not observer.listeners
        assert not n["bot"].fence.local_error
        assert {p.name: p.read_bytes() for p in n["bot"].fence.directory.iterdir() if p.is_file()} == snapshot[0]
    async with n["maker"]() as session:
        assert await session.scalar(text("SELECT count(*) FROM trades")) == 1


@pytest.mark.parametrize("fault", ["authority-json", "authority-binding", "component", "key", "key-mode"])
async def test_capture_pre_fence_custody_failure_restore_cannot_certify(native, fault):
    from polycopy.shadow_controller import operate
    n = native
    await n["backend"].close()
    n["backend"].close = _noop_close
    authority_path = Path(n["settings"].shadow_capture_authority_path)
    key_path = Path(n["settings"].shadow_capture_private_key_path)
    original_authority, original_key = authority_path.read_bytes(), key_path.read_bytes()
    cfg = n["settings"]
    if fault == "authority-json":
        authority_path.write_text("{broken")
    elif fault in ("authority-binding", "component"):
        authority = json.loads(original_authority)
        field = "generationId" if fault == "authority-binding" else "componentSha256"
        authority["binding"][field] = "wrong" if fault == "authority-binding" else "0" * 64
        authority_path.write_text(n["a"].canonical(authority))
        cfg = cfg.model_copy(update={"shadow_capture_authority_pin": n["a"].digest(authority)})
    elif fault == "key":
        key_path.write_bytes(Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    else:
        key_path.chmod(0o644)
    assert capture.install_factory(n["maker"], cfg) is n["maker"]
    assert (Path(cfg.shadow_capture_fence_path) / "capture-failed.json").is_file()
    authority_path.write_bytes(original_authority)
    key_path.write_bytes(original_key)
    key_path.chmod(0o600)
    assert capture.install_factory(n["maker"], n["settings"]) is n["maker"]
    with pytest.raises(ValueError, match="denial latch"):
        await operate(n["settings"], "fence", database_url=n["url"])


async def test_capture_wrong_spool_cannot_deny_unrelated_custody(native, tmp_path):
    n = native
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    # Copying even valid public target bytes cannot redirect them to another path.
    source = Path(n["settings"].shadow_capture_fence_path)
    (unrelated / "capture-custody.json").write_bytes((source / "capture-custody.json").read_bytes())
    (unrelated / "capture-custody.json").chmod(0o600)
    cfg = n["settings"].model_copy(update={"shadow_capture_fence_path": str(unrelated),
                                         "shadow_capture_authority_pin": "0" * 64})
    before = {p.name: p.read_bytes() for p in unrelated.iterdir()}
    assert capture.install_factory(n["maker"], cfg) is n["maker"]
    assert {p.name: p.read_bytes() for p in unrelated.iterdir()} == before
    assert not (source / "capture-failed.json").exists()


@pytest.mark.parametrize("workers", [["backend:a"], ["bot:a"], ["backend:a", "bot:a", "bot:a"],
                                      ["backend:a", "bot:a", "watchdog:a"],
                                      ["backend:a", "bot:bad/identity"]])
async def test_capture_invalid_roster(native, workers):
    n = native
    authority = json.loads(Path(n["settings"].shadow_capture_authority_path).read_text())
    authority["binding"]["expectedWorkers"] = workers
    Path(n["settings"].shadow_capture_authority_path).write_text(n["a"].canonical(authority))
    cfg = n["settings"].model_copy(update={"shadow_capture_authority_pin": n["a"].digest(authority)})
    assert capture.install_factory(n["maker"], cfg) is n["maker"]
    assert capture.status()["state"] == "FAILED"


async def test_capture_wrong_key(native):
    n = native
    Path(n["settings"].shadow_capture_private_key_path).write_bytes(
        Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    assert capture.install_factory(n["maker"], n["settings"]) is n["maker"]
    assert capture.status()["state"] == "FAILED"


@pytest.mark.parametrize("fault", ["signing", "partialappend"])
async def test_capture_append_failure_source_commit(native, monkeypatch, fault):
    n = native
    f = n["bot"].fence
    if fault == "signing":
        def broken(*args):
            raise OSError("disposable signing failure")
        monkeypatch.setattr(n["f"].A, "sign", broken)
    else:
        # Actual SQLite insert abort, not a post-success status stub.
        f.db.execute("CREATE TRIGGER fail_append BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'disposable append failure'); END")
    async with n["bot"]() as session:
        session.add(make_trade(n))
        await session.commit()
    async with n["maker"]() as session:
        assert await session.scalar(text("SELECT count(*) FROM trades")) == 1
    assert f.local_error
    if fault == "signing":
        # Signing the ERROR record fails too: restart must still refuse closure.
        assert (f.directory / "capture-failed.json").is_file()
        assert capture.install_factory(n["maker"], n["settings"]) is n["maker"]
        from polycopy.shadow_controller import operate
        with pytest.raises(ValueError, match="denial latch"):
            await operate(n["settings"], "freeze", database_url=n["url"])
    with pytest.raises(ValueError):
        f.complete()


@pytest.mark.parametrize("fault", ["badsignature", "tornjournal"])
async def test_capture_journal_restart_rejects(native, fault):
    n = native
    f = n["bot"].fence
    row = json.loads(f.db.execute("SELECT body FROM events WHERE seq=1").fetchone()[0])
    if fault == "badsignature":
        import base64
        row["signature"] = base64.b64encode(bytes(64)).decode()
        body = n["a"].canonical(row)
    else:
        body = '{"seq":'
    f.db.execute("UPDATE events SET body=? WHERE seq=1", (body,))
    assert capture.install_factory(n["maker"], n["settings"]) is n["maker"]
    assert capture.status()["state"] == "FAILED"


async def test_capture_checkpoint_mismatch(native):
    n = native
    async with n["bot"]() as session:
        session.add(make_trade(n))
        await session.commit()
    await recover(n)
    async with n["maker"]() as session:
        await session.execute(text("UPDATE trades SET size=22"))
        await session.commit()
    with pytest.raises(ValueError, match="source population"):
        await close_freeze(n)
    assert n["bot"].fence.state()["error"]


@pytest.mark.parametrize("delta,expected", [(0, 1), (1, 0)])
async def test_capture_native_exact_population_endpoint(native, delta, expected):
    n = native
    async with n["bot"]() as session:
        session.add(make_trade(n, at=n["end"] + timedelta(microseconds=delta)))
        await session.commit()
    await recover(n)
    population = await close_freeze(n)
    assert population["payload"]["count"] == expected
    checkpoint = population["payload"]["query"]["sourceSQLCheckpoint"]
    assert checkpoint["readOnly"] == "on" and checkpoint["isolation"] == "repeatable read"
    assert checkpoint["snapshot"]


async def test_capture_native_late_source_fixed_ids(native):
    n = native
    async with n["bot"]() as session:
        session.add(make_trade(n))
        await session.commit()
    await recover(n)
    population = await close_freeze(n)
    async with n["bot"]() as session:
        session.add(make_trade(n, event="late:source", at=n["end"] + timedelta(microseconds=1)))
        await session.commit()
    await recover(n)
    assert n["bot"].fence.state()["population"] == population
    def step(reader, adapter):
        d = n["f"].FencedDrain(n["settings"].shadow_capture_drain_path, n["bot"].fence, adapter, read_clock=lambda: n["clock"][0])
        try:
            result = d.step()
            assert set(result["perId"]) == {"event:1"}
            assert result["counts"]["PENDING"] == 1
        finally:
            d.close()
    await read_operation(n["url"], step)


@pytest.mark.parametrize("fault", ["missing", "conflicting", "duplicate"])
async def test_capture_native_audit_denials(native, fault):
    from polycopy.execution.service import _record_skip
    n = native
    async with n["bot"]() as session:
        trade = make_trade(n)
        session.add(trade)
        await session.commit()
        signal = Signal(wallet_id=trade.wallet_id, market_id=trade.market_id,
                        source_trade_id=trade.polymarket_trade_id, asset_id="7",
                        side="BUY", outcome="Yes", source_price="0.5",
                        t0_traded_at=trade.traded_at, t1_detected_at=trade.ingested_at)
        session.add(signal)
        await session.commit()
        await recover(n)
        await close_freeze(n)
        await _record_skip(session, signal, reason="stale_signal", now=n["end"], order_kwargs={})
        await session.commit()
        if fault == "missing":
            await session.execute(text("DELETE FROM decision_log"))
        elif fault == "conflicting":
            await session.execute(text("UPDATE decision_log SET action='wrong_action'"))
        else:
            await session.execute(text("INSERT INTO decision_log(actor,action,context,created_at) SELECT actor,action,context,created_at FROM decision_log"))
        await session.commit()
    def step(reader, adapter):
        d = n["f"].FencedDrain(n["settings"].shadow_capture_drain_path, n["bot"].fence, adapter, read_clock=lambda: n["clock"][0])
        try:
            with pytest.raises(ValueError, match="audit"):
                d.step()
            with pytest.raises(ValueError):
                d.write_seal_journals(n["settings"].shadow_capture_handoff_path)
        finally:
            d.close()
    await read_operation(n["url"], step)


async def test_capture_controller_real_lifecycle(native):
    from polycopy.shadow_controller import operate
    n = native
    assert (await operate(n["settings"], "status", database_url=n["url"]))["state"] == "ACTIVE"
    # A controller never enrolls or impersonates a worker ACK.
    n["clock"][0] = (n["end"] + timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
    await operate(n["settings"], "close", database_url=n["url"])
    assert await operate(n["settings"], "fence", database_url=n["url"]) is None
    n["backend"].control()
    n["bot"].control()
    assert await operate(n["settings"], "fence", database_url=n["url"])
    result = await operate(n["settings"], "freeze", database_url=n["url"])
    assert result["payload"]["count"] == 0


async def test_capture_native_row_savepoint_failure(native):
    from sqlalchemy.exc import IntegrityError
    n = native
    async with n["bot"]() as session:
        async with session.begin_nested():
            session.add(make_trade(n))
            await session.flush()
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(make_trade(n))
                await session.flush()
        await session.commit()
    await recover(n)
    assert (await close_freeze(n))["payload"]["count"] == 1


async def test_capture_native_process_death_commit_recovery(native, tmp_path):
    import asyncio
    import subprocess
    import sys
    n = native
    old = n["bot"]
    await old.close()
    old.close = _noop_close
    cfg = n["settings"].model_copy(update={"shadow_capture_worker": "bot:a",
        "shadow_capture_service": "bot", "database_url": n["url"]})
    path = tmp_path / "child-config.json"
    path.write_text(cfg.model_dump_json())
    script = '''import asyncio,json,os,sys
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from polycopy.config import Settings
from polycopy.models import Trade
from datetime import datetime,timezone
from polycopy.shadow_capture import install_factory,CaptureFactory
async def run():
 cfg=Settings(_env_file=None,**json.load(open(sys.argv[1])))
 assert '/proc/' in cfg.database_url
 e=create_async_engine(cfg.database_url)
 f=install_factory(async_sessionmaker(e,expire_on_commit=False),cfg)
 assert isinstance(f,CaptureFactory)
 async with f() as s:
  s.add(Trade(polymarket_trade_id='process:committed',wallet_id=int(sys.argv[2]),market_id=int(sys.argv[3]),asset_id='7',side='BUY',outcome='Yes',price='0.5',size='10',traded_at=datetime.now(timezone.utc)))
  await s.commit()
 os._exit(23)
asyncio.run(run())
'''
    p = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(path),
        str(n["wallet"].id), str(n["market"].id)], capture_output=True, text=True, check=False)
    assert p.returncode == 23, p.stdout + p.stderr
    n["bot"] = n["install"]("bot:a")
    assert n["bot"].fence.status()["outstanding"] == 1
    await recover(n)
    assert n["bot"].fence.status()["committed"] == 1
    before = len(n["bot"].fence.events())
    await recover(n)
    assert len(n["bot"].fence.events()) == before
    assert (await close_freeze(n))["payload"]["count"] == 1


async def test_capture_source_flush_error_not_swallowed(native):
    from sqlalchemy.exc import IntegrityError
    n = native
    for maker in (n["maker"], n["bot"]):
        async with maker() as session:
            trade = make_trade(n)
            trade.traded_at = None
            session.add(trade)
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()


@pytest.mark.parametrize("missing", range(4))
async def test_capture_multi_instance_enrollment_and_each_ack(native, tmp_path, missing):
    n = native
    authority = json.loads(n["a"].canonical(n["authority"]))
    roster = ["backend:a", "backend:b", "bot:a", "bot:b"]
    authority["binding"]["expectedWorkers"] = roster
    authority["installations"] = {w: {"installationSha256": "5" * 64,
        "approvedUtc": authority["approvedUtc"]} for w in roster}
    path = Path(n["settings"].shadow_capture_authority_path)
    path.write_text(n["a"].canonical(authority))
    cfg = n["settings"].model_copy(update={"shadow_capture_authority_pin": n["a"].digest(authority),
        "shadow_capture_fence_path": str(tmp_path / "multi-fence"),
        "shadow_capture_health_path": str(tmp_path / "multi-health.json")})
    n["clock"][0] = authority["approvedUtc"]
    factories = []
    try:
        for worker in roster:
            factory = capture.install_factory(n["maker"], cfg.model_copy(update={
                "shadow_capture_worker": worker, "shadow_capture_service": worker.split(":")[0]}))
            assert isinstance(factory, capture.CaptureFactory)
            factories.append(factory)
        f = factories[0].fence
        assert f.state()["workers"] == set(roster)
        n["clock"][0] = (n["end"] + timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
        f.close_epoch()
        for i, factory in enumerate(factories):
            if i != missing:
                factory.control()
        assert f.complete() is None
        assert f.status()["missingAcknowledgements"] == [roster[missing]]
        # Controller restart cannot fill in the missing worker ACK.
        reopened = n["f"].Fence(cfg.shadow_capture_fence_path, authority["binding"],
            authority=authority, authority_pin=n["a"].digest(authority), private_key=f.private_key)
        try:
            assert reopened.complete() is None
        finally:
            reopened.close()
        factories[missing].control()
        assert f.complete()
    finally:
        for factory in factories:
            await factory.close()


@pytest.mark.parametrize("fault", ["owner", "directory"])
async def test_capture_key_custody_type_owner(native, fault):
    n = native
    path = Path(n["settings"].shadow_capture_private_key_path)
    if fault == "owner":
        os.chown(path, 65534, 65534)
    else:
        path.unlink()
        path.mkdir(mode=0o600)
    assert capture.install_factory(n["maker"], n["settings"]) is n["maker"]
    assert capture.status()["state"] == "FAILED"


async def test_capture_stale_spool_binding_refused(native):
    n = native
    before = list(n["bot"].fence.events())
    authority = json.loads(n["a"].canonical(n["authority"]))
    authority["binding"]["runId"] = "different-approved-run"
    Path(n["settings"].shadow_capture_authority_path).write_text(n["a"].canonical(authority))
    cfg = n["settings"].model_copy(update={"shadow_capture_run_id": "different-approved-run",
        "shadow_capture_authority_pin": n["a"].digest(authority)})
    assert capture.install_factory(n["maker"], cfg) is n["maker"]
    assert list(n["bot"].fence.events()) == before


@pytest.mark.parametrize("missing", range(4))
async def test_capture_missing_instance_enrollment_restart(native, tmp_path, missing):
    n = native
    authority = json.loads(n["a"].canonical(n["authority"]))
    roster = ["backend:a", "backend:b", "bot:a", "bot:b"]
    authority["binding"]["expectedWorkers"] = roster
    authority["installations"] = {w: {"installationSha256": "5" * 64,
        "approvedUtc": authority["approvedUtc"]} for w in roster}
    Path(n["settings"].shadow_capture_authority_path).write_text(n["a"].canonical(authority))
    cfg = n["settings"].model_copy(update={"shadow_capture_authority_pin": n["a"].digest(authority),
        "shadow_capture_fence_path": str(tmp_path / "missing-enrollment"),
        "shadow_capture_health_path": str(tmp_path / "missing-health.json")})
    n["clock"][0] = authority["approvedUtc"]
    factories = []
    try:
        for i, worker in enumerate(roster):
            if i != missing:
                factory = capture.install_factory(n["maker"], cfg.model_copy(update={
                    "shadow_capture_worker": worker, "shadow_capture_service": worker.split(":")[0]}))
                assert isinstance(factory, capture.CaptureFactory)
                factories.append(factory)
        f = factories[0].fence
        assert f.state()["workers"] == set(roster) - {roster[missing]}
        n["clock"][0] = n["binding"]["window"]["startUtc"]
        async with factories[0]() as session:
            session.add(make_trade(n))
            await session.commit()
        assert f.local_error
        reopened = n["f"].Fence(cfg.shadow_capture_fence_path, authority["binding"],
            authority=authority, authority_pin=n["a"].digest(authority), private_key=f.private_key)
        try:
            with pytest.raises(ValueError):
                reopened.complete()
        finally:
            reopened.close()
        assert (f.directory / "capture-failed.json").is_file()
    finally:
        for factory in factories:
            await factory.close()

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sqlite3
import sys
import tarfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from test_update import ota
from test_web import login

from bell.config import load_config
from bell.recovery import (
    RecoveryError,
    create_portable_backup,
    extract_and_validate_backup,
    restore_portable_backup,
)
from bell.safety import evaluate_fire
from bell.upgrade import PreservationError, checkpoint, inventory, rollback, verify_site
from bell.web import create_app


def rewrite_archive(source: Path, output: Path, change) -> None:
    with tarfile.open(source) as original, tarfile.open(output, 'w:gz') as target:
        for member in original.getmembers():
            if member.isfile():
                data = original.extractfile(member).read()
                data = change(member.name, data)
                if data is None:
                    continue
                member.size = len(data)
                target.addfile(member, io.BytesIO(data))
            else:
                target.addfile(member)


def test_all_custom_uploads_roundtrip_and_storage_paths_stay_local(config_tree, tmp_path):
    cfg = load_config(config_tree)
    uploads = {"Custom Mass.MP3": b'mp3-original', "archive/music.flac": b'flac-original',
               "custom.data": b'unknown-original', ".class-bell.wav.restore": b'not-a-temporary-file'}
    for name, data in uploads.items():
        target = cfg.sounds_path / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(data)
    archive = create_portable_backup(cfg, tmp_path / 'backups')
    extracted = extract_and_validate_backup(archive, tmp_path / 'sandbox')
    assert extracted.sounds_path == (tmp_path / 'sandbox/sounds').resolve()
    for name, data in uploads.items():
        assert (extracted.sounds_path / name).read_bytes() == data
        (cfg.sounds_path / name).write_bytes(b'edited after backup')
    restore_portable_backup(archive, config_tree.parent, tmp_path / 'before-restore')
    for name, data in uploads.items():
        assert (cfg.sounds_path / name).read_bytes() == data
    assert load_config(config_tree).sounds_path == cfg.sounds_path


def test_backup_checksums_missing_audio_and_stored_absolute_paths(config_tree, tmp_path):
    cfg = load_config(config_tree)
    archive = create_portable_backup(cfg, tmp_path / 'backups')
    changed = tmp_path / 'changed.tar.gz'
    rewrite_archive(archive, changed, lambda name, data: b'corrupt' if name == 'sounds/class-bell.wav' else data)
    with pytest.raises(RecoveryError, match='checksum'):
        extract_and_validate_backup(changed, tmp_path / 'tampered')
    # Legacy backups lack hashes: they must still validate against the archive's own audio.
    def hostile(name, data):
        if name == 'manifest.json':
            return json.dumps({'schema': 1, 'product': 'bell-system'}).encode()
        if name == 'sounds/class-bell.wav':
            return None
        if name == 'config/settings.yaml':
            return data.replace(b'sounds_dir: sounds', f'sounds_dir: {cfg.sounds_path.as_posix()}'.encode())
        return data
    rewrite_archive(archive, changed, hostile)
    with pytest.raises(RecoveryError, match='missing or unreadable'):
        extract_and_validate_backup(changed, tmp_path / 'missing')


@pytest.mark.parametrize('name', ['../escape', 'sounds/../../escape', 'sounds\\escape', 'sounds/C:escape', '/absolute', 'config/bell.env'])
def test_hostile_archive_paths_fail_before_writes(name, tmp_path):
    archive = tmp_path / 'attack.tar.gz'
    with tarfile.open(archive, 'w:gz') as output:
        member = tarfile.TarInfo(name)
        member.size = 1
        output.addfile(member, io.BytesIO(b'x'))
    with pytest.raises(RecoveryError, match='unexpected path'):
        extract_and_validate_backup(archive, tmp_path / 'sandbox')
    assert not (tmp_path / 'escape').exists()


def test_legacy_backup_does_not_remove_newer_mp3(config_tree, tmp_path):
    cfg = load_config(config_tree)
    archive = create_portable_backup(cfg, tmp_path / 'backups')
    legacy = tmp_path / 'legacy.tar.gz'
    rewrite_archive(archive, legacy, lambda name, data: json.dumps({'schema': 1, 'product': 'bell-system'}).encode() if name == 'manifest.json' else data)
    custom = cfg.sounds_path / 'new.mp3'
    custom.write_bytes(b'preserve older-backup omission')
    restore_portable_backup(legacy, config_tree.parent, tmp_path / 'restore-backups')
    assert custom.read_bytes() == b'preserve older-backup omission'


def test_full_checkpoint_restores_credentials_uploads_and_database(config_tree, tmp_path):
    cfg = load_config(config_tree)
    cfg.state_path.mkdir(exist_ok=True)
    (config_tree / 'bell.env').write_bytes(b'credentials must survive exactly')
    (cfg.sounds_path / 'custom.MP3').write_bytes(b'original upload')
    database = cfg.state_path / 'custom.sqlite3'
    with closing(sqlite3.connect(database)) as db:
        db.execute('CREATE TABLE custom (value TEXT)')
        db.execute("INSERT INTO custom VALUES ('original')")
        db.commit()
    snapshot = tmp_path.parent / (tmp_path.name + '-checkpoint')
    checkpoint(config_tree.parent, snapshot)
    verify_site(config_tree.parent, snapshot)
    (config_tree / 'bell.env').write_bytes(b'damaged')
    (cfg.sounds_path / 'custom.MP3').unlink()
    with closing(sqlite3.connect(database)) as db:
        db.execute('DROP TABLE custom')
        db.commit()
    with pytest.raises(PreservationError, match='changed site'):
        verify_site(config_tree.parent, snapshot)
    rollback(config_tree.parent, snapshot)
    assert (config_tree / 'bell.env').read_bytes() == b'credentials must survive exactly'
    assert (cfg.sounds_path / 'custom.MP3').read_bytes() == b'original upload'
    with closing(sqlite3.connect(database)) as db:
        assert db.execute('SELECT value FROM custom').fetchone()[0] == 'original'


def test_checkpoint_change_and_corruption_fail_closed(config_tree, tmp_path, monkeypatch):
    cfg = load_config(config_tree)
    cfg.state_path.mkdir(exist_ok=True)
    snapshot = tmp_path.parent / (tmp_path.name + '-checkpoint')
    original = shutil.copy2
    def changed(source, target, *args, **kwargs):
        result = original(source, target, *args, **kwargs)
        if Path(source).name == 'calendar.yaml':
            Path(source).write_bytes(Path(source).read_bytes() + b'\n# concurrent edit\n')
        return result
    monkeypatch.setattr('bell.upgrade.shutil.copy2', changed)
    with pytest.raises(PreservationError, match='changed during checkpoint'):
        checkpoint(config_tree.parent, snapshot)
    assert not (snapshot / 'checkpoint.json').exists()


def test_maintenance_blocks_manual_override_and_web_edits(config_tree, tmp_path, monkeypatch):
    guard = tmp_path / 'upgrade-incomplete'
    monkeypatch.setattr('bell.safety.MAINTENANCE_MARKER', guard)
    monkeypatch.setattr('bell.web.MAINTENANCE_MARKER', guard)
    client = TestClient(create_app(config_tree, password='test'))
    login(client)
    guard.write_text('checkpoint', encoding='utf-8')
    cfg = load_config(config_tree)
    decision = evaluate_fire(datetime.now(ZoneInfo('America/Denver')), cfg.safety,
                             cfg.sounds_path / 'class-bell.wav', 0, manual=True, override_hours=True)
    assert not decision and 'maintenance' in decision.reason
    assert client.post('/calendar/bulk', data={}).status_code == 503
    assert 'Upgrade maintenance' in str(client.get('/operations/snapshot').json())


@pytest.fixture
def transaction_module(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, 'ota_updater', ota)
    spec = importlib.util.spec_from_file_location('transaction_under_test', Path(__file__).parents[1] / 'deploy/upgrade_transaction.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'GUARD', tmp_path / 'systemd/persistent/guard.conf')
    monkeypatch.setattr(module, 'PROBE', tmp_path / 'systemd/runtime/probe.conf')
    monkeypatch.setattr(module, 'MANAGED_PATHS', (tmp_path / 'managed.service',))
    monkeypatch.setattr(module, '_check_maintenance_window', lambda *_: None)
    monkeypatch.setattr(module, '_wait_healthy', lambda: None)
    return module


def test_failed_activation_and_interrupted_recovery_restore_same_checkpoint(config_tree, tmp_path, monkeypatch, transaction_module):
    tx = transaction_module
    app = config_tree.parent
    (app / 'state').mkdir(exist_ok=True)
    old = app / 'releases/old'
    old.mkdir(parents=True)
    try:
        (app / 'current').symlink_to('releases/old', target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    tx.MANAGED_PATHS[0].write_bytes(b'old service')
    (app / 'sounds/custom.mp3').write_bytes(b'original')
    actions = []
    def run(command, **kwargs):
        actions.append(command)
        if len(command) > 2 and command[1].endswith('upgrade.py'):
            action = command[2]
            saved = Path(command[-1])
            {'checkpoint': checkpoint, 'verify': verify_site, 'rollback': rollback}[action](app, saved)
    monkeypatch.setattr(tx, '_run', run)
    if os.name == "nt":
        def windows_test_link(target, link):
            assert link.is_symlink() and link.is_relative_to(app)
            link.unlink()
            link.symlink_to(target, target_is_directory=True)
        monkeypatch.setattr(tx, "_atomic_symlink", windows_test_link)
    transaction = tmp_path.parent / (tmp_path.name + '-transaction')
    tx.begin(app, transaction, Path(sys.executable), Path(__file__).parents[1] / 'bell/upgrade.py')
    assert tx.marker(app).exists() and 'ExecCondition=' in tx.GUARD.read_text()
    (app / 'sounds/custom.mp3').write_bytes(b'candidate corrupted audio')
    tx.MANAGED_PATHS[0].write_bytes(b'new service')
    with pytest.raises(PreservationError):
        tx.finish(app, transaction)
    assert tx.marker(app).exists()
    # Simulate restarting the recovery tool after a process/power interruption.
    tx.recover(app, transaction)
    assert (app / 'sounds/custom.mp3').read_bytes() == b'original'
    assert tx.MANAGED_PATHS[0].read_bytes() == b'old service'
    assert not tx.marker(app).exists() and not tx.PROBE.exists()
    assert json.loads((transaction / 'transaction.json').read_text())['phase'] == 'rolled_back'
    assert any('stop' in command for command in actions)


def test_candidate_cannot_silently_drop_state_but_additive_schema_is_allowed(config_tree, tmp_path):
    cfg = load_config(config_tree)
    cfg.state_path.mkdir(exist_ok=True)
    database = cfg.state_path / 'custom.sqlite3'
    with closing(sqlite3.connect(database)) as db:
        db.execute('CREATE TABLE records (value TEXT)')
        db.execute("INSERT INTO records VALUES ('keep')")
        db.commit()
    saved = tmp_path.parent / (tmp_path.name + '-checkpoint')
    checkpoint(config_tree.parent, saved)
    with closing(sqlite3.connect(database)) as db:
        db.execute('ALTER TABLE records ADD COLUMN extra TEXT')
        db.execute("INSERT INTO records VALUES ('new', 'extra')")
        db.commit()
    verify_site(config_tree.parent, saved)
    with closing(sqlite3.connect(database)) as db:
        db.execute("DELETE FROM records WHERE value='keep'")
        db.commit()
    with pytest.raises(PreservationError, match='SQLite data'):
        verify_site(config_tree.parent, saved)


def test_corrupt_checkpoint_cannot_start_rollback(config_tree, tmp_path):
    cfg = load_config(config_tree)
    cfg.state_path.mkdir(exist_ok=True)
    saved = tmp_path.parent / (tmp_path.name + '-checkpoint')
    checkpoint(config_tree.parent, saved)
    before = inventory(config_tree)
    (saved / 'config/settings.yaml').write_text('corrupted')
    with pytest.raises(PreservationError, match='checksum'):
        rollback(config_tree.parent, saved)
    assert inventory(config_tree) == before


def test_interrupted_restore_leaves_transmission_guard(config_tree, tmp_path):
    cfg = load_config(config_tree)
    saved = create_portable_backup(cfg, tmp_path / 'backups')
    def power_loss():
        raise SystemExit('simulated process termination')
    with pytest.raises(SystemExit):
        restore_portable_backup(saved, config_tree.parent, tmp_path / 'restore-backups', reload_callback=power_loss)
    assert (cfg.state_path / '.restore-incomplete').exists()
    with pytest.raises(RecoveryError, match='interrupted restore'):
        restore_portable_backup(saved, config_tree.parent, tmp_path / 'restore-backups')

"""Run the real installer shell with isolated paths and simulated OS/package/service boundaries."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bell.upgrade import inventory

ROOT = Path(__file__).parents[1]


@pytest.mark.skipif(os.name != 'posix', reason='Actual Linux installer harness runs in CI')
@pytest.mark.parametrize('failure', ['', 'validation', 'health'])
def test_installer_preserves_existing_site_and_rolls_back(config_tree, tmp_path, failure):
    source = tmp_path / 'source'
    source.mkdir()
    for name in ('bell', 'deploy', 'docs', 'scripts'):
        shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('pyproject.toml', 'README.md'):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(config_tree, source / 'config')
    shutil.copytree(config_tree.parent / 'sounds', source / 'sounds')
    app = tmp_path / 'appliance'
    shared = app / 'shared'
    shared.mkdir(parents=True)
    shutil.copytree(config_tree, shared / 'config')
    shutil.copytree(config_tree.parent / 'sounds', shared / 'sounds')
    (shared / 'state').mkdir()
    (shared / 'logs').mkdir()
    for name in ('config', 'sounds', 'state', 'logs'):
        (app / name).symlink_to('shared/' + name, target_is_directory=True)
    (shared / 'config/bell.env').write_text('BELL_UI_PASSWORD=site-secret\n')
    (shared / 'sounds/Custom.MP3').write_bytes(b'custom-upload')
    (shared / 'config/calendar.yaml').write_text((shared / 'config/calendar.yaml').read_text() + '\n# site-specific calendar\n')
    old = app / 'releases/old'
    old.mkdir(parents=True)
    (app / 'current').symlink_to('releases/old', target_is_directory=True)
    updater, units = tmp_path / 'updater', tmp_path / 'units'
    units.mkdir()
    lib = tmp_path / 'lib'
    before = {name: inventory(shared / name) for name in ('config', 'sounds')}
    script = (source / 'deploy/install.sh').read_text()
    for old_text, new_text in [('/opt/bell', str(app)), ('/var/backups/bell-system', str(tmp_path / 'backups')),
                               ('/var/lib/bell-updater', str(updater)), ('/usr/local/lib/bell-system', str(lib)),
                               ('/etc/systemd/system', str(units))]:
        script = script.replace(old_text, new_text)
    (source / 'deploy/install.sh').write_text(script)
    fake = tmp_path / 'bin'
    fake.mkdir()
    wrapper = fake / 'python3'
    wrapper.write_text('#!' + sys.executable + '\n' + r'''
import importlib.util, os, pathlib, shutil, subprocess, sys
args = sys.argv[1:]
if args[:2] == ['-m', 'venv']:
    target = pathlib.Path(args[2]) / 'bin'
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(__file__, target / 'python')
    sys.exit(0)
if args[:2] == ['-m', 'pip']:
    sys.exit(0)
if args[:2] == ['-m', 'bell.service']:
    sys.exit(1 if os.environ['FAIL_POINT'] == 'validation' else 0)
if args and args[0].endswith('upgrade_transaction.py'):
    sys.path.insert(0, str(pathlib.Path(args[0]).parent))
    spec = importlib.util.spec_from_file_location('test_transaction', args[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = pathlib.Path(os.environ['HARNESS_ROOT'])
    module.GUARD = root / 'persistent/guard.conf'
    module.PROBE = root / 'runtime/probe.conf'
    module.MANAGED_PATHS = tuple(root / 'units' / name for name in ['bell-system.service', 'bell-update.service', 'bell-update.path'])
    module._check_maintenance_window = lambda seconds: None
    def healthy():
        if os.environ['FAIL_POINT'] == 'health' and os.readlink(root / 'appliance/current') != 'releases/old':
            raise module.UpdateError('injected readiness failure')
    module._wait_healthy = healthy
    def run(command, **kwargs):
        if command[0] == '/usr/bin/systemctl':
            return subprocess.CompletedProcess(command, 0, '', '')
        result = subprocess.run([os.environ['REAL_PYTHON'], *command[1:]], capture_output=True, text=True)
        if result.returncode:
            raise module.UpdateError(result.stderr)
        return result
    module._run = run
    sys.argv = args
    sys.exit(module.main())
os.execv(os.environ['REAL_PYTHON'], [os.environ['REAL_PYTHON'], *args])
''')
    wrapper.chmod(0o755)
    runuser = fake / 'runuser'
    runuser.write_text('#!/bin/sh\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n')
    runuser.chmod(0o755)
    for name in ('chown', 'systemctl', 'useradd'):
        path = fake / name
        path.write_text('#!/bin/sh\nexit 0\n')
        path.chmod(0o755)
    identity = fake / 'id'
    identity.write_text('#!/bin/sh\necho 0\n')
    identity.chmod(0o755)
    install = fake / 'install'
    install.write_text('#!' + sys.executable + '\n' + '''import os,sys
args=[]
values=iter(sys.argv[1:])
for value in values:
    if value in ('-o','-g'):
        next(values)
    else:
        args.append(value)
os.execv('/usr/bin/install',['/usr/bin/install',*args])
''')
    install.chmod(0o755)
    env = {**os.environ, 'PATH': str(fake) + ':' + os.environ['PATH'], 'REAL_PYTHON': sys.executable,
           'PYTHONPATH': str(ROOT), 'HARNESS_ROOT': str(tmp_path), 'FAIL_POINT': failure,
           'BELL_RELEASE_VERSION': 'v0.9.0', 'BELL_RELEASE_COMMIT': 'f' * 40}
    result = subprocess.run(['bash', str(source / 'deploy/install.sh')], env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == (1 if failure else 0), result.stdout + result.stderr
    assert {name: inventory(shared / name) for name in before} == before
    assert not (app / '.upgrade-incomplete').exists()
    assert (os.readlink(app / 'current') == 'releases/old') == bool(failure)

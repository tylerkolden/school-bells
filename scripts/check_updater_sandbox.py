"""Exercise the updater sandbox and real privilege drop without touching an appliance."""
from __future__ import annotations

import configparser
import os
import pwd
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run this isolated systemd check as root")
    unit = configparser.ConfigParser(interpolation=None)
    unit.optionxform = str
    unit.read(Path(__file__).resolve().parents[1] / "deploy/bell-update.service")
    service = unit["Service"]
    assert service["NoNewPrivileges"] == "yes"
    assert service["RestrictSUIDSGID"] == "true"
    assert service["SystemCallArchitectures"] == "native"
    account = pwd.getpwnam("nobody")
    # /run is visible through PrivateTmp. Only this disposable directory is writable.
    with tempfile.TemporaryDirectory(prefix="bell-updater-check-", dir="/run") as directory:
        os.chown(directory, account.pw_uid, account.pw_gid)
        os.chmod(directory, 0o700)
        command = ["systemd-run", "--wait", "--pipe", "--collect", "-p", "User=root"]
        prefixes = ("Protect", "Private", "Restrict", "SystemCall", "Capability", "Ambient", "NoNew", "Lock", "UMask")
        for key, value in service.items():
            if key.startswith(prefixes):
                command.extend(["-p", f"{key}={value}"])
        command.extend(["-p", f"ReadWritePaths={directory}"])
        probe = (
            "import os,pathlib; "
            "s=dict(line.split(':',1) for line in pathlib.Path('/proc/self/status').read_text().splitlines()); "
            f"assert os.getuid()==os.geteuid()=={account.pw_uid}; "
            f"assert os.getgid()==os.getegid()=={account.pw_gid}; "
            "assert int(s['CapEff'],16)==int(s['CapAmb'],16)==0; "
            "assert s['NoNewPrivs'].strip()=='1'; "
            "assert s['Seccomp'].strip()=='2'; "
            f"pathlib.Path({directory!r},'probe').write_text('unprivileged'); "
            "print('Identity switch, capability drop, sandbox and staged write passed')"
        )
        # Match the updater -> bash -> runuser -> Python exec chain.
        command.extend(["/usr/bin/bash", "-c", 'exec /usr/sbin/runuser -u nobody -- /usr/bin/python3 -c "$1"', "probe", probe])
        subprocess.run(command, check=True, timeout=60)
        assert Path(directory, "probe").stat().st_uid == account.pw_uid


if __name__ == "__main__":
    main()

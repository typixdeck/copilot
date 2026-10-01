"""Pause one actor-owned CDC service while the root writer owns the port.

No shell, arbitrary unit, user environment, service installation or root GUI.
The systemd client runs as the Polkit caller against that caller's user manager.
"""
from __future__ import annotations

import os
import pwd
import subprocess

UNIT = "typix-companion-cdc.service"


class CompanionError(ValueError):
    pass


class CompanionLease:
    def __init__(self, actor=None, runner=subprocess.run):
        actor = os.environ.get("PKEXEC_UID", "") if actor is None else actor
        self.uid = None
        self.was_active = False
        self.paused = False
        self.runner = runner
        if isinstance(actor, str) and actor.isascii() and actor.isdecimal() and len(actor) <= 10:
            uid = int(actor)
            if 0 < uid < 2**32 - 1:
                try:
                    self.account = pwd.getpwuid(uid)
                    self.uid = uid
                except KeyError:
                    pass

    def command(self, *args):
        env = {"PATH": "/usr/bin:/bin", "LANG": "C", "HOME": self.account.pw_dir,
               "XDG_RUNTIME_DIR": f"/run/user/{self.uid}",
               "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{self.uid}/bus"}
        return self.runner(["/usr/bin/systemctl", "--user", *args, UNIT],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=15, check=False,
                           cwd="/", env=env, user=self.uid, group=self.account.pw_gid,
                           extra_groups=[])

    def pause(self):
        if self.uid is None:
            return
        try:
            state = self.command("is-active")
            active = state.stdout.strip() in (b"active", b"reloading", b"activating")
            if not active and state.returncode in (3, 4):
                return  # Inactive/absent user service stays untouched.
            if not active or state.returncode not in (0, 3):
                raise CompanionError("companion-stop")
            self.was_active = True
            if self.command("stop").returncode != 0:
                raise CompanionError("companion-stop")
            self.paused = True
            state = self.command("is-active")
            if state.returncode not in (3, 4):
                raise CompanionError("companion-stop")
        except (OSError, subprocess.TimeoutExpired):
            raise CompanionError("companion-stop") from None

    def restore(self, board):
        if not self.was_active or not self.paused:
            return True
        try:
            # Do not start a reconnecting runtime reader against a device that
            # remains in ROM, has disappeared, or moved outside its bound path.
            if board.probe().mode != "runtime":
                return False
            if self.command("start").returncode != 0:
                return False
            self.paused = False
            return True
        except Exception:
            return False

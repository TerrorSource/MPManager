#!/usr/bin/env python3
"""
Container entrypoint: optionally become PUID/PGID, then start the app.

PUID/PGID the way the linuxserver.io images do it: hand our own folders to that
user and stop being root, so the files written on a NAS belong to you. Nothing
outside those folders is touched. Without PUID the container stays root.
"""
import os


def drop_privileges(*folders):
    puid = os.environ.get("PUID", "").strip()
    if not puid:
        return
    if os.geteuid() != 0:
        print(f"PUID is gezet, maar de container is geen root: blijft uid {os.geteuid()}.", flush=True)
        return
    uid = int(puid)
    gid = int(os.environ.get("PGID", "").strip() or puid)
    for root in folders:
        os.makedirs(root, exist_ok=True)      # while we still may
        for dirpath, _, filenames in os.walk(root):
            for path in [dirpath] + [os.path.join(dirpath, n) for n in filenames]:
                st = os.lstat(path)
                if (st.st_uid, st.st_gid) != (uid, gid):
                    try:
                        os.lchown(path, uid, gid)
                    except OSError:
                        pass                  # some mounts (macOS, SMB) decide ownership themselves
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    os.environ["HOME"] = "/tmp"
    print(f"Draait als uid {uid} gid {gid} (PUID/PGID).", flush=True)


if __name__ == "__main__":
    drop_privileges(os.environ.get("CONFIG_DIR", "/config"), os.environ.get("BACKUP_DIR", "/data/backups"),
                    os.path.dirname(os.environ.get("KEY_PATH", "/keys/secret.key")))
    import app
    app.main()

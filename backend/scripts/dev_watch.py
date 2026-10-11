"""Dev-only reload runner (docker-compose.yml's backend command).

Windows bind mounts emit no inotify events inside the container, so
`uvicorn --reload` never fires — and watchfiles 1.2.0 ignores its own
WATCHFILES_FORCE_POLLING env default (verified 2026-10-11: an explicit
force_polling=True watch sees host edits instantly; the env-default
call sits blind while four hours of edits served stale code). Polling
must be passed EXPLICITLY, and neither uvicorn, the watchfiles CLI,
nor run_process() exposes it — so this script owns the loop: poll
the CODE dirs, restart uvicorn on any .py change. The code dirs, not
/app: a stray host .venv in the bind mount (26k files) makes a full-
tree poll take minutes per cycle over a Windows mount — the watch list
below is also what keeps any future junk dir from killing reload.
Production (docker-compose.prod.yml) runs uvicorn directly and never
touches this file.
"""

import signal
import subprocess

from watchfiles import PythonFilter, watch

CMD = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]


def _stop(proc: subprocess.Popen) -> None:
    # SIGTERM: the app's own drain path; a dev server owes no grace
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def main() -> None:
    proc = subprocess.Popen(CMD)
    try:
        for changes in watch(
            "/app/app",
            "/app/agent",
            "/app/db",
            "/app/obs",
            "/app/scripts",
            watch_filter=PythonFilter(),
            force_polling=True,
        ):
            print(f"reloading: {sorted(p for _, p in changes)}", flush=True)
            _stop(proc)
            proc = subprocess.Popen(CMD)
    finally:
        _stop(proc)


if __name__ == "__main__":
    main()

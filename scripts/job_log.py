"""Live command output with labels, timing, and optional local log copies."""

from contextlib import nullcontext
import queue
import shlex
import subprocess
import threading
import time

_OUTPUT_LOCK = threading.Lock()
HEARTBEAT_SECONDS = 30


def progress(message, label="release"):
    with _OUTPUT_LOCK:
        for line in str(message).splitlines():
            print(f"[{label}] {line}", flush=True)


def stream_command(args, *, cwd=None, env=None, log=None, label="command",
                   capture=False, show_output=True):
    args = [str(arg) for arg in args]
    description = shlex.join(args)
    progress(f"START $ {description}", label)
    started = time.monotonic()
    output = []
    destination = log.open("w", buffering=1) if log else nullcontext(None)
    with destination as stream:
        if stream:
            stream.write(f"Command: {description}\n")
        with subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1) as process:
            pending = queue.Queue(maxsize=256)
            def read_output():
                try:
                    for line in process.stdout:
                        pending.put(line)
                finally:
                    pending.put(None)
            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            while True:
                try:
                    line = pending.get(timeout=HEARTBEAT_SECONDS)
                except queue.Empty:
                    progress(f"Still running ({time.monotonic() - started:.0f}s elapsed)", label)
                    continue
                if line is None:
                    break
                if stream:
                    stream.write(line)
                if capture:
                    output.append(line)
                if show_output:
                    progress(line.rstrip("\r\n"), label)
            returncode = process.wait()
            reader.join()
    elapsed = time.monotonic() - started
    if returncode:
        if not show_output:
            progress("".join(output), label)
        progress(f"FAILED: exit {returncode} after {elapsed:.1f}s", label)
        location = f"; local log: {log}" if log else ""
        raise RuntimeError(f"Command failed (exit {returncode}): {description}{location}")
    progress(f"DONE in {elapsed:.1f}s", label)
    return "".join(output).strip()

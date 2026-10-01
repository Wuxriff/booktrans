"""Windows process operations used by the work lock and CLI agents."""
import ctypes
from ctypes import wintypes
import os
import queue
import subprocess
import threading
import time
from functools import lru_cache


@lru_cache(maxsize=1)
def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def pid_alive(pid):
    """Check a PID without os.kill(pid, 0), which terminates it on Windows."""
    kernel = _kernel()
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no such PID
            return False
        if error == 5:  # ERROR_ACCESS_DENIED: assume it is running
            return True
        raise ctypes.WinError(error)
    try:
        state = kernel.WaitForSingleObject(handle, 0)
        if state == 0:    # WAIT_OBJECT_0: exited
            return False
        if state == 258:  # WAIT_TIMEOUT: still running
            return True
        raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


def spawn_flags():
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


def attach_job(p):
    """Keep the agent's descendants reachable after the agent itself exits."""
    kernel = _kernel()
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        p._bt_job = None
        return
    proc = kernel.OpenProcess(0x0101, False, p.pid)  # SET_QUOTA | TERMINATE
    try:
        p._bt_job = job if proc and kernel.AssignProcessToJobObject(job, proc) else None
    finally:
        if proc:
            kernel.CloseHandle(proc)
        if p._bt_job is None:
            kernel.CloseHandle(job)


def stop_tree(p):
    """Terminate the job, or use taskkill when job assignment was refused."""
    kernel = _kernel()
    job = getattr(p, "_bt_job", None)
    stopped = False
    if job:
        try:
            stopped = bool(kernel.TerminateJobObject(job, 1))
        finally:
            kernel.CloseHandle(job)
            p._bt_job = None
    if not stopped:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if p.poll() is None:
        try:
            p.kill()
        except OSError:
            pass


def read_chunks(p, cmd, deadline, timeout):
    """Read a subprocess pipe without select(), which only accepts sockets here."""
    chunks = queue.Queue()

    def pump():
        try:
            while True:
                chunk = os.read(p.stdout.fileno(), 1 << 16)
                if not chunk:
                    break
                chunks.put(chunk)
        except OSError:
            pass
        finally:
            chunks.put(None)

    threading.Thread(target=pump, daemon=True).start()
    while True:
        left = deadline - time.time()
        if left <= 0:
            raise subprocess.TimeoutExpired(cmd, timeout)
        try:
            chunk = chunks.get(timeout=min(1, left))
        except queue.Empty:
            if p.poll() is not None and chunks.empty():
                break  # A descendant may still hold the pipe open.
            continue
        if chunk is None:
            break
        yield chunk

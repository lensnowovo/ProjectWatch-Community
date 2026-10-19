"""Windows child process tree owned by a kill-on-close Job Object.

CPython's venv redirector creates another python.exe. Terminating only the
redirector is insufficient. Assign while suspended, before it can spawn children.
No process discovery, taskkill by name, or management of an existing process.
"""

import os
import subprocess


class WindowsChild:
    def __init__(self, command, *, env, log):
        import _winapi
        import ctypes
        import msvcrt
        from ctypes import wintypes as wt

        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong), ('flags', wt.DWORD),
                        ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t), ('active', wt.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wt.DWORD), ('scheduling', wt.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
        kernel.CreateJobObjectW.restype = wt.HANDLE
        kernel.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
        kernel.SetInformationJobObject.restype = wt.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        kernel.AssignProcessToJobObject.restype = wt.BOOL
        kernel.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
        kernel.TerminateJobObject.restype = wt.BOOL
        kernel.ResumeThread.argtypes = [wt.HANDLE]
        kernel.ResumeThread.restype = wt.DWORD
        self.kernel, self.api, self.command = kernel, _winapi, command
        self.job = kernel.CreateJobObjectW(None, None)
        self.process = None
        if not self.job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        thread = None
        try:
            if not kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            with open(os.devnull, 'rb') as null:
                stdin, stdout = msvcrt.get_osfhandle(null.fileno()), msvcrt.get_osfhandle(log.fileno())
                startup = subprocess.STARTUPINFO()
                startup.dwFlags = subprocess.STARTF_USESTDHANDLES
                startup.hStdInput, startup.hStdOutput, startup.hStdError = stdin, stdout, stdout
                # Inherit only the explicitly listed log and null handles.
                startup.lpAttributeList = {'handle_list': [stdin, stdout]}
                os.set_handle_inheritable(stdin, True)
                os.set_handle_inheritable(stdout, True)
                try:
                    self.process, thread, self.pid, _ = _winapi.CreateProcess(None, subprocess.list2cmdline(command),
                        None, None, True, subprocess.CREATE_NO_WINDOW | 0x4, env, None, startup)
                finally:
                    os.set_handle_inheritable(stdin, False)
                    os.set_handle_inheritable(stdout, False)
            if not kernel.AssignProcessToJobObject(self.job, self.process):
                raise ctypes.WinError(ctypes.get_last_error())
            if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            if self.process is not None:
                _winapi.TerminateProcess(self.process, 1)
            self.close()
            raise
        finally:
            if thread is not None:
                _winapi.CloseHandle(thread)

    def poll(self):
        if self.api.WaitForSingleObject(self.process, 0) == 258:
            return None
        return self.api.GetExitCodeProcess(self.process)

    def wait(self, timeout):
        if self.api.WaitForSingleObject(self.process, int(timeout * 1000)) == 258:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.api.GetExitCodeProcess(self.process)

    def terminate(self):
        import ctypes
        if not self.kernel.TerminateJobObject(self.job, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    kill = terminate

    def close(self):
        if self.job is not None:
            self.api.CloseHandle(self.job)
            self.job = None
        if self.process is not None:
            self.api.CloseHandle(self.process)
            self.process = None

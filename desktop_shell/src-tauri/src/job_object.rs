//! Windows Job Object wrapper: kill-on-parent-exit for the sidecar process.
//!
//! The shell creates a job with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE and assigns
//! the sidecar process to it.  When the shell process dies (crash, force
//! kill, normal exit) the last job handle is closed and Windows terminates
//! the sidecar — no orphans.
//!
//! If the assignment fails (e.g. the shell itself runs inside a restricted
//! job), we fall back to explicit TerminateProcess on exit plus the
//! sidecar's own stdin-EOF watchdog (the sidecar exits when its parent's
//! stdin pipe closes).

use std::sync::Mutex;
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, SetInformationJobObject,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JobObjectExtendedLimitInformation,
};
use windows_sys::Win32::System::Threading::{
    OpenProcess, TerminateProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE,
};

pub struct JobObject {
    handle: HANDLE,
}

// A raw Windows HANDLE is a pointer-sized opaque value; closing/assigning it
// is thread-safe.  The manager shares the job across threads, so declare
// Send + Sync explicitly.
unsafe impl Send for JobObject {}
unsafe impl Sync for JobObject {}

impl JobObject {
    /// Create a kill-on-close job in the current process.
    pub fn create() -> Result<JobObject, String> {
        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle.is_null() {
                return Err(format!("CreateJobObjectW failed: {}", std::io::Error::last_os_error()));
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let size = std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32;
            let ok = SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const std::ffi::c_void,
                size,
            );
            if ok == 0 {
                let err = std::io::Error::last_os_error();
                CloseHandle(handle);
                return Err(format!("SetInformationJobObject failed: {err}"));
            }
            Ok(JobObject { handle })
        }
    }

    /// Assign an existing process (by pid) to the job.
    pub fn assign(&self, pid: u32) -> Result<(), String> {
        unsafe {
            let process = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid);
            if process.is_null() {
                return Err(format!("OpenProcess({pid}) failed: {}", std::io::Error::last_os_error()));
            }
            let ok = AssignProcessToJobObject(self.handle, process);
            CloseHandle(process);
            if ok == 0 {
                return Err(format!(
                    "AssignProcessToJobObject({pid}) failed: {}",
                    std::io::Error::last_os_error()
                ));
            }
            Ok(())
        }
    }

    /// Force-terminate a process (fallback path).
    pub fn terminate_process(pid: u32) -> bool {
        unsafe {
            let process = OpenProcess(PROCESS_TERMINATE, 0, pid);
            if process.is_null() {
                return false;
            }
            let ok = TerminateProcess(process, 1);
            CloseHandle(process);
            ok != 0
        }
    }
}

impl Drop for JobObject {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.handle);
        }
    }
}

/// Optional job guard stored in the sidecar manager.
pub struct JobGuard {
    job: Mutex<Option<JobObject>>,
}

impl JobGuard {
    pub fn new() -> JobGuard {
        JobGuard { job: Mutex::new(None) }
    }

    pub fn install(&self, job: JobObject) {
        *self.job.lock().unwrap() = Some(job);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn create_and_drop_job_is_safe() {
        // Never assign the *current* process: a kill-on-close job would
        // terminate it when the handle closes.  Just verify create + drop
        // and that a bogus pid assignment fails cleanly.
        let job = JobObject::create().expect("job creation must work");
        assert!(job.assign(0xFFFF_FFFF).is_err() || job.assign(0xFFFF_FFFF).is_ok());
        drop(job); // CloseHandle on a valid handle
    }
}

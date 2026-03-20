use anyhow::Result;
use std::path::Path;

// ══════════════════════════════════════════════════════════════════════════════
// PLATFORM-SPECIFIC IMMUTABILITY
// ══════════════════════════════════════════════════════════════════════════════

// ── Linux: FS_IMMUTABLE_FL via ioctl (chattr +i) ────────────────────────────

#[cfg(target_os = "linux")]
mod platform {
    use anyhow::{Context, Result};
    use std::os::unix::io::AsRawFd;
    use std::path::Path;

    const FS_IOC_SETFLAGS: u64 = 0x40086602;
    const FS_IOC_GETFLAGS: u64 = 0x80086601;
    const FS_IMMUTABLE_FL: u32 = 0x00000010;

    pub fn set_immutable(path: &Path, immutable: bool) -> Result<()> {
        use std::fs::OpenOptions;
        use std::os::unix::fs::OpenOptionsExt;

        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(path)
            .with_context(|| format!("opening {:?} for ioctl", path))?;

        let fd = file.as_raw_fd();
        let mut flags: u32 = 0;

        unsafe {
            if libc::ioctl(fd, FS_IOC_GETFLAGS as libc::c_ulong, &mut flags) != 0 {
                return Ok(());
            }
        }

        if immutable {
            flags |= FS_IMMUTABLE_FL;
        } else {
            flags &= !FS_IMMUTABLE_FL;
        }

        unsafe {
            if libc::ioctl(fd, FS_IOC_SETFLAGS as libc::c_ulong, &flags) != 0 {
                return Ok(());
            }
        }

        Ok(())
    }

    pub fn is_immutable(path: &Path) -> Result<bool> {
        use std::fs::OpenOptions;

        let file = OpenOptions::new()
            .read(true)
            .open(path)
            .with_context(|| format!("opening {:?} to check immutable", path))?;

        let fd = file.as_raw_fd();
        let mut flags: u32 = 0;

        unsafe {
            if libc::ioctl(fd, FS_IOC_GETFLAGS as libc::c_ulong, &mut flags) != 0 {
                return Ok(false);
            }
        }

        Ok(flags & FS_IMMUTABLE_FL != 0)
    }

    pub fn platform_name() -> &'static str { "linux (FS_IMMUTABLE_FL)" }
}

// ── macOS: chflags UF_IMMUTABLE (chflags uchg) ─────────────────────────────

#[cfg(target_os = "macos")]
mod platform {
    use anyhow::{Context, Result};
    use std::ffi::CString;
    use std::path::Path;

    // BSD file flags
    const UF_IMMUTABLE: u32 = 0x00000002; // user immutable (uchg)

    extern "C" {
        fn chflags(path: *const libc::c_char, flags: libc::c_uint) -> libc::c_int;
    }

    fn get_flags(path: &Path) -> Result<u32> {
        use std::os::unix::fs::MetadataExt;
        let meta = std::fs::metadata(path)
            .with_context(|| format!("stat {:?}", path))?;
        // st_flags is available on macOS via MetadataExt
        Ok(meta.st_flags())
    }

    pub fn set_immutable(path: &Path, immutable: bool) -> Result<()> {
        let current = match get_flags(path) {
            Ok(f) => f,
            Err(_) => return Ok(()), // silently skip if stat fails
        };

        let new_flags = if immutable {
            current | UF_IMMUTABLE
        } else {
            current & !UF_IMMUTABLE
        };

        if new_flags == current {
            return Ok(());
        }

        let c_path = CString::new(path.to_string_lossy().as_bytes())
            .with_context(|| format!("invalid path {:?}", path))?;

        unsafe {
            if chflags(c_path.as_ptr(), new_flags) != 0 {
                // silently skip if not supported or permission denied
                return Ok(());
            }
        }

        Ok(())
    }

    pub fn is_immutable(path: &Path) -> Result<bool> {
        match get_flags(path) {
            Ok(flags) => Ok(flags & UF_IMMUTABLE != 0),
            Err(_) => Ok(false),
        }
    }

    pub fn platform_name() -> &'static str { "macos (UF_IMMUTABLE / chflags uchg)" }
}

// ── Windows: read-only attribute + ACL deny write ───────────────────────────

#[cfg(target_os = "windows")]
mod platform {
    use anyhow::{Context, Result};
    use std::path::Path;

    pub fn set_immutable(path: &Path, immutable: bool) -> Result<()> {
        let meta = std::fs::metadata(path)
            .with_context(|| format!("reading metadata {:?}", path))?;
        let mut perms = meta.permissions();
        perms.set_readonly(immutable);
        std::fs::set_permissions(path, perms)
            .with_context(|| format!("setting permissions {:?}", path))?;

        // For stronger protection, use icacls to deny write access
        if immutable {
            let _ = std::process::Command::new("icacls")
                .args([
                    &path.to_string_lossy().to_string(),
                    "/deny", "*S-1-1-0:(W,D)",  // deny Everyone write+delete
                    "/Q",
                ])
                .output();
        } else {
            let _ = std::process::Command::new("icacls")
                .args([
                    &path.to_string_lossy().to_string(),
                    "/remove:d", "*S-1-1-0",     // remove deny entries
                    "/Q",
                ])
                .output();
        }

        Ok(())
    }

    pub fn is_immutable(path: &Path) -> Result<bool> {
        let meta = std::fs::metadata(path)
            .with_context(|| format!("reading metadata {:?}", path))?;
        Ok(meta.permissions().readonly())
    }

    pub fn platform_name() -> &'static str { "windows (read-only + NTFS ACL deny)" }
}

// ── Fallback: unsupported platforms ─────────────────────────────────────────

#[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
mod platform {
    use anyhow::Result;
    use std::path::Path;

    pub fn set_immutable(_path: &Path, _immutable: bool) -> Result<()> {
        // No immutability support — versioning still works, just unprotected
        Ok(())
    }

    pub fn is_immutable(_path: &Path) -> Result<bool> {
        Ok(false)
    }

    pub fn platform_name() -> &'static str { "unsupported (no immutability)" }
}

// ══════════════════════════════════════════════════════════════════════════════
// PUBLIC API (delegates to platform module)
// ══════════════════════════════════════════════════════════════════════════════

/// Set or clear the immutable flag on a file.
///
/// - **Linux**: FS_IMMUTABLE_FL via ioctl (chattr +i/-i)
/// - **macOS**: UF_IMMUTABLE via chflags (chflags uchg/nouchg)
/// - **Windows**: read-only attribute + NTFS ACL deny write/delete
///
/// Silently succeeds on unsupported filesystems or insufficient permissions.
pub fn set_immutable(path: &Path, immutable: bool) -> Result<()> {
    platform::set_immutable(path, immutable)
}

/// Check if a file currently has the immutable flag set.
pub fn is_immutable(path: &Path) -> Result<bool> {
    platform::is_immutable(path)
}

/// Return a human-readable description of the immutability mechanism on this platform.
pub fn platform_name() -> &'static str {
    platform::platform_name()
}

// ══════════════════════════════════════════════════════════════════════════════
// PASSPHRASE + UNLOCK (platform-independent)
// ══════════════════════════════════════════════════════════════════════════════

/// Unlock manager — temporarily removes immutable flags for a TTL duration.
pub struct UnlockSession {
    pub unlocked_paths: Vec<std::path::PathBuf>,
    pub expires_at: std::time::Instant,
}

impl UnlockSession {
    pub fn new(ttl_seconds: u64) -> Self {
        Self {
            unlocked_paths: Vec::new(),
            expires_at: std::time::Instant::now()
                + std::time::Duration::from_secs(ttl_seconds),
        }
    }

    pub fn is_expired(&self) -> bool {
        std::time::Instant::now() >= self.expires_at
    }

    /// Re-lock all previously unlocked paths.
    pub fn relock_all(&self) -> Result<()> {
        for path in &self.unlocked_paths {
            if path.exists() {
                let _ = set_immutable(path, true);
            }
        }
        Ok(())
    }
}

/// Verify a passphrase against the stored Argon2 hash.
pub fn verify_passphrase(passphrase: &str, hash: &str) -> bool {
    use argon2::{Argon2, PasswordHash, PasswordVerifier};
    if hash.is_empty() {
        return false;
    }
    let parsed = match PasswordHash::new(hash) {
        Ok(h) => h,
        Err(_) => return false,
    };
    Argon2::default()
        .verify_password(passphrase.as_bytes(), &parsed)
        .is_ok()
}

/// Hash a passphrase with Argon2 for storage in config.
pub fn hash_passphrase(passphrase: &str) -> Result<String> {
    use argon2::{
        password_hash::{PasswordHasher, SaltString},
        Argon2,
    };
    use rand_core::OsRng;
    let salt = SaltString::generate(&mut OsRng);
    let hash = Argon2::default()
        .hash_password(passphrase.as_bytes(), &salt)
        .map_err(|e| anyhow::anyhow!("argon2 error: {e}"))?
        .to_string();
    Ok(hash)
}

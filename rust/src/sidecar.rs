//! Owns the on-disk `.tyo3/` sidecar layout (SPEC §11.2) and all crash-safe
//! writes into it. The single place any `.tyo3/...` path is constructed.
//
// INVARIANT: every write goes through `write_atomic` (temp-then-rename), so an
// interrupted write leaves the prior valid file (§11.3.3). Source files are
// never touched (§11.3.4).

use std::path::{Path, PathBuf};
use std::{fs, io};

pub struct Sidecar {
    root: PathBuf,
}

impl Sidecar {
    /// Bind to `<project_root>/.tyo3`. Does NOT create anything; a project with
    /// no sidecar is valid (§11.3.6). Creation is lazy, on first write.
    pub fn new(project_root: &Path) -> Self {
        Sidecar {
            root: project_root.join(".tyo3"),
        }
    }

    pub fn exists(&self) -> bool {
        self.root.is_dir()
    }

    // ── Canonical paths (the ONLY place these are spelled) ──
    pub fn config_path(&self) -> PathBuf {
        self.root.join("config.toml")
    }

    pub fn config_local_path(&self) -> PathBuf {
        self.root.join("config.local.toml")
    }

    pub fn secrets_path(&self) -> PathBuf {
        self.root.join("secrets.toml")
    }

    pub fn identity_db_path(&self) -> PathBuf {
        self.root.join("identity.db")
    }

    pub fn authored_dir(&self, layer: &str) -> PathBuf {
        self.root.join("authored").join(layer)
    }

    pub fn cache_dir(&self, layer: &str) -> PathBuf {
        self.root.join("cache").join(layer)
    }

    pub fn gitignore_path(&self) -> PathBuf {
        self.root.join(".gitignore")
    }

    /// Ensure a directory under the sidecar exists (created lazily before a write).
    pub fn ensure_dir(&self, dir: &Path) -> io::Result<()> {
        fs::create_dir_all(dir)
    }

    /// Crash-safe write: write to `<path>.tmp`, fsync, rename over `path`
    /// (§11.3.3). The shared primitive every sidecar writer MUST use.
    pub fn write_atomic(&self, path: &Path, bytes: &[u8]) -> io::Result<()> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let tmp = path.with_extension("tmp");
        {
            let mut f = fs::File::create(&tmp)?;
            use io::Write;
            f.write_all(bytes)?;
            f.sync_all()?;
        }
        fs::rename(&tmp, path)
    }
}

#[cfg(test)]
mod tests {
    use super::Sidecar;

    #[test]
    fn new_on_fresh_dir_creates_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let sidecar = Sidecar::new(dir.path());

        assert!(!sidecar.exists());
        assert!(!dir.path().join(".tyo3").exists());
    }

    #[test]
    fn write_atomic_round_trips_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let sidecar = Sidecar::new(dir.path());
        let path = sidecar.identity_db_path();

        sidecar.write_atomic(&path, b"hello").unwrap();

        assert_eq!(std::fs::read(&path).unwrap(), b"hello");
    }

    #[test]
    fn interrupted_write_leaves_prior_file_intact() {
        let dir = tempfile::tempdir().unwrap();
        let sidecar = Sidecar::new(dir.path());
        let path = sidecar.identity_db_path();

        sidecar.write_atomic(&path, b"prior").unwrap();
        std::fs::write(path.with_extension("tmp"), b"interrupted").unwrap();

        assert_eq!(std::fs::read(&path).unwrap(), b"prior");
    }
}

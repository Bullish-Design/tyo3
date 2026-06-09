// Emit linker flags for the test binary (cdylib resolves Python at runtime,
// but test binaries need libpython linked explicitly).
fn main() {
    // Use python3-config to get the correct link flags for this Python.
    let output = std::process::Command::new("python3-config")
        .args(["--ldflags", "--embed"])
        .output();

    if let Ok(output) = output {
        if output.status.success() {
            let flags = String::from_utf8_lossy(&output.stdout);
            for token in flags.split_whitespace() {
                if let Some(lib) = token.strip_prefix("-l") {
                    println!("cargo:rustc-link-lib={}", lib);
                } else if let Some(path) = token.strip_prefix("-L") {
                    println!("cargo:rustc-link-search=native={}", path);
                }
            }
        }
    }

    // Also link against libpython3.13 as fallback
    println!("cargo:rustc-link-lib=python3.13");
}

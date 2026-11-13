"""Purpose: provide private artifact storage and bounded, shell-free subprocess execution."""

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path


# Inputs: directory path. Outputs: the same Path. Purpose: establish private storage.
# Dependencies: local filesystem ownership and permission metadata.
def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not path.is_dir()
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise ValueError(f"Directory must be owned by this user and have mode 0700: {path}")
    return path


# Inputs: artifact path and JSON value. Outputs: none. Purpose: atomically save private artifacts.
# Dependencies: private_dir, JSON serialization, and same-directory atomic replacement.
def write_json(path, value):
    path = Path(path)
    private_dir(path.parent)
    if path.is_symlink():
        raise ValueError("Artifact paths cannot be symbolic links")
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


# Inputs: JSON artifact path. Outputs: decoded value. Purpose: read external artifacts.
# Dependencies: UTF-8 filesystem reads and the standard JSON decoder.
def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# Inputs: file path. Outputs: hexadecimal digest. Purpose: bind artifact contents to reviews.
# Dependencies: a readable regular file and SHA-256.
def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# Inputs: argv, optional cwd/stdin, timeout. Outputs: CompletedProcess, including nonzero codes.
# Purpose: run bounded commands without shell expansion. Dependencies: installed executables.
def run(argv, *, cwd=None, input_text=None, timeout=60):
    environment = dict(os.environ)
    if str(argv[0]) == "terraform":
        for key in list(environment):
            if key.startswith(("TF_CLI_ARGS", "TF_VAR_", "PLUGIN_")) or key in {
                "TF_DATA_DIR",
                "TF_LOG",
                "TF_LOG_PATH",
                "TF_REATTACH_PROVIDERS",
            }:
                environment.pop(key)
        environment.update(TF_IN_AUTOMATION="1", TF_INPUT="0", TF_WORKSPACE="default")
    try:
        return subprocess.run(
            [str(item) for item in argv],
            cwd=cwd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
            check=False,
            umask=0o077,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Command timed out; partial output was suppressed") from None
    except OSError:
        raise RuntimeError("Command could not start; check the required executable") from None


# Inputs: private workspace. Outputs: context manager. Purpose: serialize one environment operation.
# Dependencies: POSIX advisory file locks; no lock is held between CLI invocations.
@contextmanager
def workspace_lock(workspace):
    private_dir(workspace)
    path = Path(workspace) / ".operation.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another operation is already using this workspace") from None
        yield
    finally:
        os.close(descriptor)

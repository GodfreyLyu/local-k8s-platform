<!-- Purpose: Describe the implemented artifact I/O, workspace locking, and subprocess boundaries. -->

# Artifact I/O and subprocesses

This reference describes [io_utils.py](../scripts/io_utils.py) and its callers. It applies to the Python helpers on macOS and Linux. Container startup scripts have their own shell commands.

## Private directories

`private_dir(path)` requests mode `0700` for each new directory. It creates missing parents through the same helper. The process umask can remove requested permission bits.

The helper checks the requested directory with `lstat`. It requires the current user's UID and no group or other permission bits. It rejects a symbolic link or a non-directory at that path. It does not require an exact owner permission mask, such as `0700`. It does not change existing permissions.

These checks do not validate every existing ancestor directory. The helpers do not hold open directory descriptors to protect the full path from concurrent replacement.

## JSON reads and hashes

`read_json(path)` opens the supplied path with `O_NOFOLLOW` and `O_NONBLOCK`. It rejects a final symbolic link. Non-blocking open prevents a FIFO from waiting for a writer. The opened object must be a regular file.

The function decodes UTF-8 with Python's standard JSON decoder. It can return any decoded JSON value. It does not check the file's owner, permission bits, or configuration schema. Missing-file, decoding, and JSON syntax errors propagate to the caller.

[config.py](../scripts/config.py) resolves the configuration path before calling `read_json`. A configuration symlink can therefore resolve to a regular file and be accepted. The no-follow rule applies to the path that `read_json` receives.

`sha256(path)` reads the whole file and returns a hexadecimal SHA-256 digest. It does not perform the JSON reader's symlink or file-type checks. Runtime stage-source and state checks are separate from this helper.

## Atomic JSON writes

`write_json(path, value)` performs these steps:

1. Check the parent with `private_dir`. Reject a destination symlink.
2. Create a `.write-*` temporary file in that directory. Creation requests mode `0600`.
3. Write UTF-8 JSON with sorted keys, two-space indentation, and a final newline.
4. Flush, `fsync`, and close the temporary file.
5. Recheck the destination for a symlink. Publish the file with `os.replace`.

The replacement is atomic for that file. The writer does not check the existing destination file's owner or mode. It does not make stage copying or a complete `configure` operation atomic. It does not `fsync` the parent directory, so it does not guarantee persistence after power loss.

The package builder also writes its archive directly. Archive output does not use this JSON writer or its private-directory checks.

An error after temporary-file creation triggers an unlink attempt. If cleanup fails, the original error is raised with the cleanup error as its cause. The temporary file may remain. A successful replacement consumes the temporary name; the writer does not unlink that name afterward.

Reads and writes use managed streams. If stream construction fails, the helper also closes the raw file descriptor.

The symlink checks do not make the whole path race-free. The parent directories must remain trusted during the operation.

## Workspace locking

`workspace_lock(workspace)` checks the workspace directory. It opens `.operation.lock` with `O_NOFOLLOW`. Creation requests mode `0600`. It does not revalidate an existing lock file's owner or permission bits.

The helper uses an exclusive, non-blocking `flock`. Contention raises `ValueError` immediately. Closing the descriptor releases the lock. The lock file remains on disk.

[platform.py](../scripts/platform.py) holds this lock during workspace preparation and the selected action. The lock coordinates cooperating processes using that file. It is not a cluster-wide lock. The standalone validator and package builder do not acquire it.

## Subprocess execution

`run(argv, *, cwd=None, input_text=None, timeout=60)` expects a non-empty argument sequence. It converts each argument to a string and passes the list to `subprocess.run`. It does not use a shell wrapper. Shell expansion, pipes, and redirection are not added by this helper. An invoked program can still launch a shell itself.

The helper passes the optional working directory and stdin text. With no working directory, the child uses the current directory. Without `input_text`, the helper does not redirect stdin. It captures stdout and stderr as text. It returns `CompletedProcess` even when the command exits nonzero. Callers decide which exit codes are acceptable.

The child receives a `077` umask. This masks permissions on new files and directories. It does not repair existing permissions.

The runner has no executable allowlist. It adds no CPU, memory, output-size, or process-group limits. Captured output is held in memory. The timeout is a per-call setting, not a deadline for the whole CLI operation. It does not guarantee that all descendant processes have stopped.

### Terraform environment handling

The runner starts from the current environment. Special filtering applies only when `str(argv[0]) == "terraform"`:

| Action | Variables |
|---|---|
| Remove by prefix | `TF_CLI_ARGS*`, `TF_VAR_*`, `PLUGIN_*`. |
| Remove exact names | `TF_DATA_DIR`, `TF_LOG`, `TF_LOG_PATH`, `TF_REATTACH_PROVIDERS`. |
| Set fixed values | `TF_IN_AUTOMATION=1`, `TF_INPUT=0`, `TF_WORKSPACE=default`. |

Other variables remain inherited. This includes `PATH` and `TF_CLI_CONFIG_FILE`. An absolute Terraform executable path does not trigger this filter. Other executables receive the inherited environment without this filtering.

### Timeouts and errors

| Caller | Timeout |
|---|---:|
| `io_utils.run` default | 60 seconds. |
| Runtime Kubernetes reads through `_get` | 20 seconds. |
| Runtime Terraform init, plan, and apply through `_terraform` | 600 seconds. |
| Saved-plan decoding with `terraform show` | 60 seconds. |
| Credential kubectl and OpenSSL calls | 60 seconds. |
| Standalone `validate.py` Terraform wrapper | 120 seconds. |

Runtime kubectl calls also set `--request-timeout=10s`. Credential kubectl calls set `--request-timeout=15s`. These are separate API request limits.

In `run`, a timeout raises `RuntimeError` with a fixed message. Partial output is suppressed. An `OSError` from starting or running the command also becomes a fixed `RuntimeError`. These errors suppress the original exception context. A nonzero exit alone does not raise either error.

The helper does not log or redact captured output. Runtime `_terraform` writes raw stdout and stderr to private JSON logs after `run` returns. If `run` raises, that command log is not written by `_terraform`.

### Standalone validation

[validate.py](../scripts/validate.py) has a separate subprocess wrapper. It also uses an argument list, captured text output, `check=False`, and a `077` child umask.

It removes all inherited `TF_*` and `PLUGIN_*` variables. It then sets automation defaults, `TF_LOG=TRACE`, a private `TF_LOG_PATH`, and `CHECKPOINT_DISABLE=1`. It records command results and TRACE diagnostics in the report directory. Timeout and startup failures become structured results with a cause and no partial stdout or stderr.

The validator avoids Kubernetes requests. Provider installation can still contact the registry. It does not provide network isolation.

## Failure reporting

`platform.py` and `validate.py` report caught exceptions as JSON with `str(error)`. They do not print exception chains. A chained temporary-file cleanup failure may therefore be absent from the CLI error text.

After a write failure, inspect the affected private directory for `.write-*` files. Leftover files in stage directories can block later configuration. The helpers do not scan for or remove stale temporary files at startup. Keep credential values and raw diagnostic output out of shared reports.

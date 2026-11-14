"""Purpose: verify private JSON storage, atomic publication, and failure cleanup offline."""

import errno
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import io_utils


class JsonStorageTests(unittest.TestCase):
    # Inputs: none. Outputs: private artifact fixture. Dependencies: local temporary storage.
    # Purpose: keep every filesystem mutation outside the repository and real workspaces.
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "artifact.json"
        self.original = '{"status": "original"}\n'
        self.path.write_text(self.original, encoding="utf-8")
        self.path.chmod(0o600)

    # Inputs: representative JSON values. Outputs: unchanged values and private formatted files.
    # Purpose: preserve the existing JSON interface and serialization format. Dependencies: JSON codec.
    def test_json_round_trip_preserves_values_format_and_permissions(self):
        values = [{"z": [True, None, 1.5], "a": "日本語"}, [1, "two"], "text", 4, False, None]
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(io_utils.write_json(str(self.path), value))
                self.assertEqual(io_utils.read_json(str(self.path)), value)
                self.assertEqual(
                    self.path.read_text(encoding="utf-8"),
                    json.dumps(value, indent=2, sort_keys=True) + "\n",
                )
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
                self.assertFalse(list(self.root.glob(".write-*")))

    # Inputs: nested absent directories under a permissive umask. Outputs: private new directories.
    # Purpose: prevent mkdir(parents=True) from exposing intermediate storage. Dependencies: POSIX modes.
    def test_private_dir_creates_every_missing_parent_privately(self):
        destination = self.root / "first" / "second" / "third"
        previous = os.umask(0o022)
        try:
            self.assertEqual(io_utils.private_dir(destination), destination)
        finally:
            os.umask(previous)
        for directory in (destination, destination.parent, destination.parent.parent):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    # Inputs: unsafe existing leaf directories. Outputs: refusal without changing their targets or modes.
    # Purpose: preserve existing ownership and symlink boundaries. Dependencies: lstat metadata.
    def test_private_dir_rejects_links_files_and_public_directories(self):
        target = self.root / "target"
        target.mkdir(mode=0o700)
        link = self.root / "directory-link"
        link.symlink_to(target, target_is_directory=True)
        dangling = self.root / "dangling-directory"
        dangling.symlink_to(self.root / "missing-directory", target_is_directory=True)
        public = self.root / "public"
        public.mkdir()
        public.chmod(0o755)
        for path in (link, dangling, self.path, public):
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                io_utils.private_dir(path)
        self.assertTrue(link.is_symlink())
        self.assertFalse((self.root / "missing-directory").exists())
        self.assertEqual(stat.S_IMODE(public.stat().st_mode), 0o755)
        self.assertEqual(self.path.read_text(), self.original)

    # Inputs: existing and dangling JSON symlinks. Outputs: read/write refusal with intact targets.
    # Purpose: apply the artifact symlink rule to both operations. Dependencies: filesystem links.
    def test_json_reads_and_writes_reject_symlinks(self):
        for target in (self.path, self.root / "missing.json"):
            link = self.root / f"link-{target.name}"
            link.symlink_to(target)
            for operation in (io_utils.read_json, lambda path: io_utils.write_json(path, {})):
                with self.subTest(target=target.name, operation=operation):
                    with self.assertRaisesRegex(ValueError, "symbolic"):
                        operation(link)
            self.assertTrue(link.is_symlink())
        self.assertEqual(self.path.read_text(), self.original)
        self.assertFalse((self.root / "missing.json").exists())

    # Inputs: a file replaced by a symlink immediately before open. Outputs: refused read.
    # Purpose: test no-follow behavior at the actual filesystem boundary. Dependencies: os.open.
    def test_read_rejects_a_symlink_inserted_at_open(self):
        target = self.root / "other.json"
        target.write_text('{"status": "other"}', encoding="utf-8")
        real_open = os.open

        # Inputs: open arguments. Outputs: actual descriptor or no-follow error.
        # Purpose: reproduce a pathname change before the syscall. Dependencies: real os.open.
        def swap_before_open(path, flags, *args, **kwargs):
            self.path.unlink()
            self.path.symlink_to(target)
            return real_open(path, flags, *args, **kwargs)

        with patch.object(io_utils.os, "open", side_effect=swap_before_open):
            with self.assertRaisesRegex(ValueError, "symbolic"):
                io_utils.read_json(self.path)
        self.assertEqual(json.loads(target.read_text()), {"status": "other"})

    # Inputs: invalid JSON or invalid UTF-8. Outputs: the original decoder errors.
    # Purpose: preserve read error behavior while closing the opened stream. Dependencies: JSON/UTF-8.
    def test_read_preserves_decode_errors(self):
        for content, error in (
            (b'{"broken":', json.JSONDecodeError),
            (b'"\xff"', UnicodeDecodeError),
        ):
            self.path.write_bytes(content)
            with self.subTest(content=content), self.assertRaises(error):
                io_utils.read_json(self.path)
        with self.assertRaises(FileNotFoundError):
            io_utils.read_json(self.root / "missing.json")

    # Inputs: a directory and an unopened FIFO. Outputs: prompt rejection as non-regular artifacts.
    # Purpose: keep special files from blocking JSON reads. Dependencies: POSIX non-blocking open/fstat.
    def test_read_rejects_non_regular_files(self):
        fifo = self.root / "input.fifo"
        os.mkfifo(fifo, mode=0o600)
        for path, error in ((self.root, IsADirectoryError), (fifo, ValueError)):
            with self.subTest(path=path.name), self.assertRaises(error):
                io_utils.read_json(path)

    # Inputs: a destination changed to a symlink during serialization. Outputs: refusal and intact link.
    # Purpose: recheck a mutable pathname before publication. Dependencies: real JSON encoder and symlinks.
    def test_write_rejects_a_symlink_inserted_before_publication(self):
        target = self.root / "other.json"
        target.write_text(self.original, encoding="utf-8")
        real_dump = json.dump

        # Inputs: encoder arguments. Outputs: serialized bytes and a replaced destination entry.
        # Purpose: reproduce a destination change during I/O. Dependencies: real json.dump.
        def swap_after_dump(*args, **kwargs):
            real_dump(*args, **kwargs)
            self.path.unlink()
            self.path.symlink_to(target)

        with patch.object(io_utils.json, "dump", side_effect=swap_after_dump):
            with self.assertRaisesRegex(ValueError, "symbolic"):
                io_utils.write_json(self.path, {"status": "new"})
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(), self.original)
        self.assertFalse(list(self.root.glob(".write-*")))

    # Inputs: failures before atomic replacement. Outputs: old JSON and no leftover temporary files.
    # Purpose: protect the previously published artifact on every write failure. Dependencies: injected I/O errors.
    def test_failed_writes_preserve_old_json_and_remove_temporary_files(self):
        failures = (
            ("dump", io_utils.json, TypeError("serialization failed")),
            ("fsync", io_utils.os, OSError("file synchronization failed")),
            ("replace", io_utils.os, OSError("replacement failed")),
        )
        for name, module, error in failures:
            with self.subTest(operation=name):
                self.path.write_text(self.original, encoding="utf-8")
                with patch.object(module, name, side_effect=error):
                    with self.assertRaises(type(error)) as caught:
                        io_utils.write_json(self.path, {"status": "new"})
                self.assertIs(caught.exception, error)
                self.assertEqual(self.path.read_text(), self.original)
                self.assertFalse(list(self.root.glob(".write-*")))

    # Inputs: a value that fails after partially serializing. Outputs: unchanged destination and no debris.
    # Purpose: cover real encoder failure rather than only mocked errors. Dependencies: json.dump.
    def test_partial_serialization_does_not_publish_or_leave_debris(self):
        with self.assertRaises(TypeError):
            io_utils.write_json(self.path, {"a": "already serialized", "z": object()})
        self.assertEqual(self.path.read_text(), self.original)
        self.assertFalse(list(self.root.glob(".write-*")))

    # Inputs: stream construction failure during either operation. Outputs: closed raw descriptors.
    # Purpose: prevent descriptor leaks before a stream takes ownership. Dependencies: real OS descriptors.
    def test_stream_open_failures_close_descriptors(self):
        for operation in (io_utils.read_json, lambda path: io_utils.write_json(path, {})):
            opened = []
            real_open = os.open

            # Inputs: file-open arguments. Outputs: tracked descriptor.
            # Purpose: inspect descriptor lifetime after failure. Dependencies: real os.open.
            def track_open(*args, **kwargs):
                opened_descriptor = real_open(*args, **kwargs)
                opened.append(opened_descriptor)
                return opened_descriptor

            with self.subTest(operation=operation):
                try:
                    with (
                        patch.object(io_utils.os, "open", side_effect=track_open),
                        patch.object(io_utils.os, "fdopen", side_effect=OSError("stream failed")),
                    ):
                        with self.assertRaisesRegex(OSError, "stream failed"):
                            operation(self.path)
                    self.assertTrue(opened)
                    for descriptor in opened:
                        with self.assertRaises(OSError) as caught:
                            os.fstat(descriptor)
                        self.assertEqual(caught.exception.errno, errno.EBADF)
                finally:
                    for descriptor in opened:
                        try:
                            os.close(descriptor)
                        except OSError as error:
                            if error.errno != errno.EBADF:
                                raise
                self.assertEqual(self.path.read_text(), self.original)
                self.assertFalse(list(self.root.glob(".write-*")))

    # Inputs: a normal write observed during fsync and replace. Outputs: complete, synchronized JSON.
    # Purpose: prove old contents remain visible until publication. Dependencies: real fsync/replace.
    def test_write_flushes_and_syncs_before_atomic_publication(self):
        value = {"status": "new", "values": list(range(100))}
        synchronized = []
        real_fsync = os.fsync
        real_replace = os.replace

        # Inputs: pending file descriptor. Outputs: normal synchronization result.
        # Purpose: observe flushed bytes before publication. Dependencies: real fsync.
        def inspect_sync(descriptor):
            self.assertEqual(self.path.read_text(), self.original)
            temporary = next(self.root.glob(".write-*"))
            self.assertEqual(json.loads(temporary.read_text()), value)
            real_fsync(descriptor)
            synchronized.append(True)

        # Inputs: temporary and published paths. Outputs: atomic replacement.
        # Purpose: require synchronization while preserving same-directory rename. Dependencies: os.replace.
        def inspect_replace(source, destination):
            self.assertEqual(synchronized, [True])
            self.assertEqual(Path(source).parent, Path(destination).parent)
            self.assertEqual(self.path.read_text(), self.original)
            real_replace(source, destination)

        with (
            patch.object(io_utils.os, "fsync", side_effect=inspect_sync),
            patch.object(io_utils.os, "replace", side_effect=inspect_replace),
        ):
            io_utils.write_json(self.path, value)
        self.assertEqual(io_utils.read_json(self.path), value)
        self.assertFalse(list(self.root.glob(".write-*")))

    # Inputs: publication failure followed by cleanup failure. Outputs: original error with cleanup cause.
    # Purpose: preserve the actionable failure and expose cleanup trouble. Dependencies: mocked unlink.
    def test_cleanup_failure_does_not_mask_the_original_error(self):
        original = TypeError("serialization failed")
        cleanup = PermissionError("temporary cleanup failed")
        with (
            patch.object(io_utils.json, "dump", side_effect=original),
            patch.object(Path, "unlink", side_effect=cleanup),
        ):
            with self.assertRaises(TypeError) as caught:
                io_utils.write_json(self.path, {})
        self.assertIs(caught.exception, original)
        self.assertIs(caught.exception.__cause__, cleanup)
        self.assertEqual(self.path.read_text(), self.original)

    # Inputs: a temporary pathname reused immediately after rename. Outputs: the new file survives.
    # Purpose: ensure successful writes never remove files they no longer own. Dependencies: os.replace.
    def test_success_does_not_unlink_a_reused_temporary_name(self):
        reused = []
        real_replace = os.replace

        # Inputs: source and destination. Outputs: completed publication and independently reused name.
        # Purpose: reproduce reuse before post-rename cleanup. Dependencies: real os.replace.
        def reuse_after_replace(source, destination):
            real_replace(source, destination)
            temporary = Path(source)
            temporary.write_text("independent file", encoding="utf-8")
            reused.append(temporary)

        with patch.object(io_utils.os, "replace", side_effect=reuse_after_replace):
            io_utils.write_json(self.path, {"status": "new"})
        self.assertEqual(io_utils.read_json(self.path), {"status": "new"})
        self.assertEqual(reused[0].read_text(), "independent file")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Cache I/O for keycap-resolve, using open flags the shell cannot reach.

    mkdir <dir>            create/verify the private cache directory
    read  <path>           write the cache to stdout; exit 0 only if it passed
    write <path> <source>  publish <source> at <path>

The whole point of this file is the flags on the two open calls.

Reading opens with O_NOFOLLOW|O_NONBLOCK and then decides everything from
fstat on that one descriptor. Checking the pathname first and opening it
afterwards leaves a window in which the name can be swapped for a symlink;
here there is no window, because the kernel refuses the symlink at open time
and every later check is made against the object actually opened.

Writing creates its temporary with O_CREAT|O_EXCL|O_NOFOLLOW, which cannot
land on a name that already exists, and keeps that descriptor open through
write, verification and fsync. The pathname is never reopened. Publication is
os.replace, which swaps the directory entry atomically and replaces a symlink
sitting at the destination rather than following it.
"""

import json
import os
import stat
import sys

MAX_BYTES = 1024 * 1024
REQUIRED_KEYS = ("byApp", "byCmd", "byRoute", "byName")
CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def ensure_dir(path):
    """A directory this user owns, that only this user may enter."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError:
        return 1

    try:
        st = os.lstat(path)  # lstat: a symlink here must fail, not be followed
    except OSError:
        return 1

    if not stat.S_ISDIR(st.st_mode):
        return 1
    if st.st_uid != os.geteuid():
        return 1
    if st.st_mode & 0o077:
        return 1
    return 0


def read_cache(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | CLOEXEC)
    except OSError:
        # Missing, or a symlink, or a fifo with no writer: all uncacheable.
        return 1

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return 1
        if st.st_uid != os.geteuid():
            return 1
        if not 0 < st.st_size <= MAX_BYTES:
            return 1

        os.set_blocking(fd, True)
        chunks = []
        total = 0
        while total < MAX_BYTES:
            try:
                chunk = os.read(fd, min(65536, MAX_BYTES - total))
            except OSError:
                return 1
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        body = b"".join(chunks)
    finally:
        os.close(fd)

    try:
        parsed = json.loads(body)
        text = body.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return 1

    if not isinstance(parsed, dict):
        return 1
    if any(key not in parsed for key in REQUIRED_KEYS):
        return 1

    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def write_cache(path, source):
    try:
        with open(source, "rb") as handle:
            body = handle.read(MAX_BYTES + 1)
    except OSError:
        return 1

    if not 0 < len(body) <= MAX_BYTES:
        return 1

    directory = os.path.dirname(path) or "."
    fd = None
    tmp = None

    try:
        for _ in range(8):
            candidate = os.path.join(
                directory, ".keycap.%d.%s" % (os.getpid(), os.urandom(8).hex())
            )
            try:
                fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | CLOEXEC,
                    0o600,
                )
                tmp = candidate
                break
            except FileExistsError:
                continue
        if fd is None:
            return 1

        view = memoryview(body)
        written = 0
        while written < len(body):
            written += os.write(fd, view[written:])

        os.fsync(fd)
        if os.fstat(fd).st_size != len(body):
            return 1

        os.close(fd)
        fd = None
        os.replace(tmp, path)
        tmp = None
        return 0
    except OSError:
        return 1
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def main(argv):
    if len(argv) >= 3 and argv[1] == "mkdir":
        return ensure_dir(argv[2])
    if len(argv) >= 3 and argv[1] == "read":
        return read_cache(argv[2])
    if len(argv) >= 4 and argv[1] == "write":
        return write_cache(argv[2], argv[3])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

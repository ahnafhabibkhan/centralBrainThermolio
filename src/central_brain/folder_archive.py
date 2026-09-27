"""Stream folder downloads without buffering the archive or its originals."""

import io
import queue
import tarfile
import threading


class _QueueWriter:
    def __init__(self, output, stopped):
        self.output = output
        self.stopped = stopped

    def write(self, data):
        if not data:
            return 0
        while not self.stopped.is_set():
            try:
                self.output.put(bytes(data), timeout=0.25)
                return len(data)
            except queue.Full:
                continue
        raise BrokenPipeError("Folder download stopped.")

    def flush(self):
        return None


def tar_stream(store, entries):
    """Yield a PAX tar archive while each original is read sequentially."""
    output = queue.Queue(maxsize=8)
    stopped = threading.Event()
    complete = object()

    def send(item):
        while not stopped.is_set():
            try:
                output.put(item, timeout=0.25)
                return
            except queue.Full:
                continue

    def build():
        try:
            with tarfile.open(fileobj=_QueueWriter(output, stopped), mode="w|",
                              format=tarfile.PAX_FORMAT) as archive:
                for entry in entries:
                    info = tarfile.TarInfo(entry["path"])
                    info.mtime = int(entry["created_at"].timestamp())
                    info.mode = 0o755 if entry["kind"] == "folder" else 0o644
                    if entry["kind"] == "folder":
                        info.type = tarfile.DIRTYPE
                        archive.addfile(info)
                        continue
                    info.size = entry["size"]
                    if "data" in entry:
                        archive.addfile(info, io.BytesIO(entry["data"]))
                        continue
                    stream = store.get(entry["object_key"])
                    try:
                        archive.addfile(info, stream)
                    finally:
                        stream.close()
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001  The response iterator must receive worker errors.
            send(exc)
        finally:
            send(complete)

    worker = threading.Thread(target=build, name="folder-download", daemon=True)
    worker.start()
    try:
        while True:
            item = output.get()
            if item is complete:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        stopped.set()

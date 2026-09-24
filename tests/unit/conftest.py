"""Environment every unit test needs before the first service import.

`services.api.main` opens its outbox at import time, on the path the container
mounts (`/outbox`). The test image has no volumes and runs as a non-root user,
so that path is not writable. `services.common.config` reads the variable once,
at import, and pytest loads this file before it collects any test module --
which is the only window in which setting it still works.

The DSN is built at the same moment and refuses to be empty, so it gets a
placeholder here. Nothing connects with it: `Pool` dials lazily, and no unit
test asks it to -- a test that needed a live database would belong in
`tests/integration/`, not here.
"""

import os
import tempfile
from pathlib import Path

os.environ.setdefault(
    "AOW_OUTBOX_PATH",
    str(Path(tempfile.gettempdir()) / "aow-unit-outbox.sqlite3"),
)
os.environ.setdefault("POSTGRES_READER_PASSWORD", "unit-tests-never-connect")
# The consumer builds the writer DSN at import time for the same reason.
os.environ.setdefault("POSTGRES_WRITER_PASSWORD", "unit-tests-never-connect")

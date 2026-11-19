import os
import tempfile

# Must run before any app module is imported, so every test run gets a fresh database.
_tmp = tempfile.mkdtemp()
os.environ["COLDSTACK_DATA_DIR"] = _tmp
os.environ["COLDSTACK_DB"] = os.path.join(_tmp, "test.db")

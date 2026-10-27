import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="moodle-sync-test-"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import moodle_sync  # noqa: E402


class CleanTests(unittest.TestCase):
    def test_unescapes_html_and_collapses_whitespace(self):
        self.assertEqual(moodle_sync.clean("  Bases &amp;  Datos \n 1 "), "Bases & Datos 1")

    def test_strips_control_characters(self):
        self.assertEqual(moodle_sync.clean("a\x00b\x1fc"), "abc")

    def test_empty_or_none_gets_placeholder(self):
        self.assertEqual(moodle_sync.clean(""), "sin_nombre")
        self.assertEqual(moodle_sync.clean(None), "sin_nombre")
        self.assertEqual(moodle_sync.clean("  \x00 "), "sin_nombre")

    def test_truncates_to_maxlen(self):
        self.assertEqual(moodle_sync.clean("x" * 500), "x" * 150)
        self.assertEqual(moodle_sync.clean("abcdef", maxlen=3), "abc")


class FlattenParamsTests(unittest.TestCase):
    def test_scalar_dict(self):
        self.assertEqual(moodle_sync.flatten_params({"a": 1, "b": "x"}), {"a": 1, "b": "x"})

    def test_list_uses_indexes(self):
        self.assertEqual(moodle_sync.flatten_params({"courseids": [5, 7]}),
                         {"courseids[0]": 5, "courseids[1]": 7})

    def test_nested_options(self):
        params = {"options": [{"name": "x", "value": 1}]}
        self.assertEqual(moodle_sync.flatten_params(params),
                         {"options[0][name]": "x", "options[0][value]": 1})

    def test_bool_becomes_int(self):
        self.assertEqual(moodle_sync.flatten_params({"a": True, "b": False}), {"a": 1, "b": 0})


class Md5FileTests(unittest.TestCase):
    def test_known_digest(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"hello")
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(moodle_sync.md5_file(f.name), "5d41402abc4b2a76b9719d911017c592")

    def test_reads_files_larger_than_one_chunk(self):
        data = b"a" * ((1 << 20) + 17)
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
        self.addCleanup(os.unlink, f.name)
        import hashlib
        self.assertEqual(moodle_sync.md5_file(f.name), hashlib.md5(data).hexdigest())


if __name__ == "__main__":
    unittest.main()

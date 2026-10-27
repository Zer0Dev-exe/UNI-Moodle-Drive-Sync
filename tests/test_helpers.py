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


class ConfigTests(unittest.TestCase):
    ENV = ["MOODLE_URL", "MOODLE_USER", "MOODLE_PASSWORD", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "TEST_INT"]

    def setUp(self):
        saved = {k: os.environ.pop(k, None) for k in self.ENV}
        self.addCleanup(lambda: [os.environ.__setitem__(k, v) for k, v in saved.items() if v is not None])
        self.addCleanup(lambda: [os.environ.pop(k, None) for k, v in saved.items() if v is None])

    def test_missing_config_lists_every_variable(self):
        self.assertEqual(moodle_sync.missing_config(False), self.ENV[:5])

    def test_dry_run_does_not_need_google(self):
        os.environ.update(MOODLE_URL="https://m", MOODLE_USER="u", MOODLE_PASSWORD="p")
        self.assertEqual(moodle_sync.missing_config(True), [])
        self.assertEqual(moodle_sync.missing_config(False), ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"])

    def test_blank_value_counts_as_missing(self):
        os.environ.update(MOODLE_URL="https://m", MOODLE_USER="  ", MOODLE_PASSWORD="p")
        self.assertEqual(moodle_sync.missing_config(True), ["MOODLE_USER"])

    def test_env_int(self):
        self.assertEqual(moodle_sync.env_int("TEST_INT"), 0)
        self.assertEqual(moodle_sync.env_int("TEST_INT", 5), 5)
        os.environ["TEST_INT"] = " 7 "
        self.assertEqual(moodle_sync.env_int("TEST_INT"), 7)

    def test_env_int_rejects_non_numbers(self):
        os.environ["TEST_INT"] = "dos"
        with self.assertRaises(SystemExit) as cm:
            moodle_sync.env_int("TEST_INT")
        self.assertIn("TEST_INT", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

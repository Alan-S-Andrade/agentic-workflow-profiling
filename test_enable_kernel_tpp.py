import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import enable_kernel_tpp as tpp


class KernelTPPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in (("balancing", "1"), ("demotion", "false"), ("watermark", "200")):
            self.put(tpp.CONTROLS[name], value)
        self.put("proc/zoneinfo", "Node 0, zone Normal, toptier 1 next_demotion_node 1\n"
                 "        demote 10\n        managed 1024\n")
        for node in (0, 1):
            self.put(f"sys/devices/system/node/node{node}/meminfo", f"Node {node} MemTotal: 1024 kB")
        self.put("sys/devices/system/node/node0/cpulist", "0-3")

    def put(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n")

    def test_stock_kernel_rejected_without_writes(self):
        (self.root / tpp.CONTROLS["watermark"]).unlink()
        with self.assertRaisesRegex(RuntimeError, "patched kernel"):
            tpp.enable(200, self.root)
        self.assertEqual((self.root / tpp.CONTROLS["balancing"]).read_text().strip(), "1")

    def test_wrong_demotion_target_rejected(self):
        self.put("proc/zoneinfo", "Node 0, zone Normal, toptier 1 next_demotion_node -1")
        with self.assertRaisesRegex(RuntimeError, "demotion target"):
            tpp.enable(200, self.root)

    def test_enable_and_repeat(self):
        tpp.enable(300, self.root)
        self.assertEqual(tpp.inspect(self.root)["controls"],
                         {"balancing": "2", "demotion": "true", "watermark": "300"})
        tpp.enable(300, self.root)

    def test_watermark_is_recomputed_after_tiering_is_enabled(self):
        with patch.object(tpp, "write_checked", wraps=tpp.write_checked) as write:
            tpp.enable(200, self.root)
        self.assertEqual([call.args for call in write.call_args_list], [
            (self.root / tpp.CONTROLS["demotion"], "true"),
            (self.root / tpp.CONTROLS["balancing"], "2"),
            (self.root / tpp.CONTROLS["watermark"], "200"),
        ])

    def test_zero_demotion_watermark_rejects_enable_and_restores_controls(self):
        self.put("proc/zoneinfo", "Node 0, zone Normal, toptier 1 next_demotion_node 1\n"
                 "        demote 0\n        managed 1024\n")
        original = tpp.inspect(self.root)["controls"]
        with self.assertRaisesRegex(RuntimeError, "watermark is still zero"):
            tpp.enable(200, self.root)
        self.assertEqual(tpp.inspect(self.root)["controls"], original)
    def test_failed_write_restores_all_previous_controls(self):
        original = tpp.inspect(self.root)["controls"]
        real_write = tpp.write_checked

        def fail_after_write(path, value):
            real_write(path, value)
            if path == self.root / tpp.CONTROLS["balancing"] and value == "2":
                raise OSError("simulated readback failure")

        with patch.object(tpp, "write_checked", side_effect=fail_after_write):
            with self.assertRaisesRegex(RuntimeError, "Rollback errors: \\[\\]"):
                tpp.enable(300, self.root)
        self.assertEqual(tpp.inspect(self.root)["controls"], original)


if __name__ == "__main__":
    unittest.main()

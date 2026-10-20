"""Admission and source-timestamp checks; no benchmark VMs are launched."""
import array
import gzip
import io
import json
import tempfile
import threading
import time
import tarfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from build_azure_launch_schedule import extract_gaps, make_schedule
from swebench_microvm_scale_experiment import WorkflowStartGate, admit_staggered, load_admission_schedule


class AdmissionTests(unittest.TestCase):
    def test_user_starts_and_cross_partition_deduplication(self):
        def turn(number, stamp, agent_stamp=None):
            calls = [{"initiator_type": "user", "timestamp": stamp}]
            if agent_stamp:
                calls.append({"initiator_type": "agent", "timestamp": agent_stamp})
            return {"turn_id": str(number), "llm_calls": calls}
        first = turn(1, "2026-06-01T00:00:00.000000000Z", "2026-06-01T00:10:00Z")
        second = turn(2, "2026-06-01T00:00:10.000000000Z")
        third = turn(3, "2026-06-02T00:00:10Z")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archives = []
            for day, turns in ((1, [first, second]), (2, [second, third])):
                archive = root / f"date.{day}.tar.gz"
                payload = gzip.compress((json.dumps({"session_id": "s", "date": str(day), "turns": turns}) + "\n").encode())
                with tarfile.open(archive, "w:gz") as tar:
                    member = tarfile.TarInfo("shard-0000.jsonl.gz")
                    member.size = len(payload)
                    tar.addfile(member, io.BytesIO(payload))
                archives.append(archive)
            values, counts, _ = extract_gaps(archives, root / "gaps.jsonl", 1)
            self.assertEqual(list(values), [10, 86400])
            self.assertEqual(counts["unique_user_turns"], 3)
            self.assertEqual(counts["duplicate_turn_records"], 1)

    def test_seeded_sampling_and_explicit_time_scaling(self):
        with tempfile.TemporaryDirectory() as temp:
            pool = Path(temp) / "gaps.jsonl"
            pool.write_text(''.join(json.dumps({"gap_seconds": v}) + "\n" for v in (5, 10, 30)))
            values = array.array("d", [5, 10, 30])
            first = make_schedule(pool, values, 8, 42, 1)
            self.assertEqual(first, make_schedule(pool, values, 8, 42, 1))
            scaled = make_schedule(pool, values, 8, 42, .1)
            self.assertAlmostEqual(scaled["sessions"][-1]["launch_offset_seconds"], first["sessions"][-1]["launch_offset_seconds"] / 10)
            path = Path(temp) / "schedule.json"
            path.write_text(json.dumps(first))
            self.assertEqual(len(load_admission_schedule(path, 8)["sessions"]), 8)
            first["sessions"][1]["launch_offset_seconds"] = -1
            path.write_text(json.dumps(first))
            with self.assertRaises(SystemExit):
                load_admission_schedule(path, 8)

    def test_next_launch_does_not_wait_for_previous_boot(self):
        gate = SimpleNamespace(lock=threading.Lock(), arrived=set())
        vms = [SimpleNamespace(session=i, launched_mono=None, done=lambda: False) for i in (1, 2)]
        launched = []
        def launch(vm, offset, epoch):
            vm.launched_mono = time.monotonic()
            launched.append((vm.session, vm.launched_mono, set(gate.arrived)))
            if vm.session == 2:
                gate.arrived.update((1, 2))
        admit_staggered(vms, [0, .04], launch, gate, threading.Event(), 1)
        self.assertEqual([row[0] for row in launched], [1, 2])
        self.assertGreaterEqual(launched[1][1] - launched[0][1], .035)
        self.assertEqual(launched[1][2], set())

    def test_memory_guard_cancels_future_admissions(self):
        gate = SimpleNamespace(lock=threading.Lock(), arrived=set())
        guard = threading.Event()
        vms = [SimpleNamespace(session=i, launched_mono=None, done=lambda: False) for i in (1, 2)]
        launched = []
        def launch(vm, offset, epoch):
            vm.launched_mono = time.monotonic()
            launched.append(vm.session)
            guard.set()
        admit_staggered(vms, [0, 10], launch, gate, guard, 1)
        self.assertEqual(launched, [1])

    def test_early_guest_failure(self):
        gate = SimpleNamespace(lock=threading.Lock(), arrived=set())
        vm = SimpleNamespace(session=1, launched_mono=None, done=lambda: True)
        def launch(vm, offset, epoch):
            vm.launched_mono = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "exited before workflow"):
            admit_staggered([vm], [0], launch, gate, threading.Event(), 1)

    def test_independent_gate_releases_before_later_guest_arrives(self):
        gate = WorkflowStartGate("127.0.0.1", [1, 2], independent=True)
        try:
            with urllib.request.urlopen(gate.url + "/1", timeout=1) as response:
                self.assertEqual(response.status, 204)
            self.assertFalse(gate.ready())
            self.assertIn(1, gate.started_at)
            self.assertNotIn(2, gate.started_at)
        finally:
            gate.close()

    def test_baseline_gate_still_waits_for_common_release(self):
        gate = WorkflowStartGate("127.0.0.1", [1])
        finished = threading.Event()
        def request():
            with urllib.request.urlopen(gate.url + "/1", timeout=2) as response:
                self.assertEqual(response.status, 204)
            finished.set()
        thread = threading.Thread(target=request)
        try:
            thread.start()
            deadline = time.monotonic() + 1
            while not gate.ready() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(gate.ready())
            self.assertFalse(finished.is_set())
            gate.released.set()
            thread.join(timeout=2)
            self.assertTrue(finished.is_set())
        finally:
            gate.close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

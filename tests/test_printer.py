"""printer tool + ada-cups-helper — diagnostics reads, bounded repair,
confirm-gate, registry drift verdicts, helper allowlist.

Card ada-printer-diagnostics-safe-repair (2026-11-23). The helper
(scripts/ada-cups-helper.py) runs on the print host; these tests import
it by path and exercise it offline — no CUPS needed.
"""
import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ADA_MEMORY_BANKS_FILE", "/nonexistent-banks.json")

from backend.tool_runner import ToolRunner  # noqa: E402
from backend import printer_client  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
_HELPER_PATH = _REPO / "scripts" / "ada-cups-helper.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location(
        "ada_cups_helper", _HELPER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


helper = _load_helper()


# ---------------------------------------------------------------- fixtures
# Real shapes captured live 2026-11-23 on tony-dell (lpstat / ipptool).

_LPSTAT_V = """\
device for HP-2777: dnssd://HP%20DeskJet%202700%20series%20%5B05C516%5D._ipp._tcp.local/?uuid=5ba42c21-ae68-7f6d-830e-600ee5b90fe
device for HP_DeskJet_2700: ipp://192.168.2.75/ipp/print
"""
_LPSTAT_P = """\
printer HP-2777 is idle.  enabled since Thu 24 Sep 2026 11:39:33 AM +07
printer HP_DeskJet_2700 is idle.  enabled since Tue 06 Oct 2026 04:14:05 PM +07
"""
_LPSTAT_A = """\
HP-2777 accepting requests since Thu 24 Sep 2026 11:39:33 AM +07
HP_DeskJet_2700 accepting requests since Tue 06 Oct 2026 04:14:05 PM +07
"""
_LPSTAT_D = "system default destination: HP_DeskJet_2700\n"

_IPP_PROBE_92 = """\
        printer-name (nameWithoutLanguage) = HP05C516
        printer-dns-sd-name (nameWithoutLanguage) = HP DeskJet 2700 series \\[05C516]
        printer-make-and-model (textWithoutLanguage) = HP DeskJet 2700 series
        printer-state (enum) = idle
        printer-state-reasons (keyword) = none
        printer-is-accepting-jobs (boolean) = true
        queued-job-count (integer) = 0
        printer-device-id (textWithoutLanguage) = MFG:HP;MDL:DeskJet 2700 series;CMD:PCL3GUI;SN:CN33BDZ0KS;S:038000C4;
        printer-uuid (uri) = urn:uuid:5ba42c21-ae68-7f6d-830e-600ee5b906fe
        marker-names (1setOf nameWithoutLanguage) = tri-color ink,black ink
        marker-types (1setOf keyword) = ink-cartridge,ink-cartridge
        marker-levels (1setOf integer) = 70,40
        marker-low-levels (1setOf integer) = 2,2
        marker-high-levels (1setOf integer) = 100,100
        status-code = successful-ok (successful-ok)
"""

_STATUS_PAYLOAD = {
    "ok": True, "op": "status", "host": "tony-dell",
    "queues": [
        {"name": "HP-2777", "state": "idle", "accepting": True,
         "device_uri": "dnssd://HP%20DeskJet%202700%20series%20%5B05C516%5D._ipp._tcp.local/?uuid=x",
         "device": {"uri": "dnssd://...", "probed": False,
                    "target": "HP%20DeskJet%202700%20series%20%5B05C516%5D._ipp._tcp.local",
                    "error": "dnssd target unresolved"},
         "jobs": [], "printer_internal_jobs": []},
        {"name": "HP_DeskJet_2700", "state": "idle", "accepting": True,
         "is_default": True,
         "device_uri": "ipp://192.168.2.75/ipp/print",
         "device": {"uri": "ipp://192.168.2.75/ipp/print",
                    "target": "192.168.2.75", "probed": True,
                    "ok": False, "error": "ipptool: Host is down"},
         "jobs": [], "printer_internal_jobs": []},
    ],
}

_VERIFIED_PROBE = {
    "uri": "ipp://192.168.2.92/ipp/print", "target": "192.168.2.92",
    "probed": True, "ok": True,
    "serial": "CN33BDZ0KS",
    "uuid": "5ba42c21-ae68-7f6d-830e-600ee5b906fe",
    "printer-dns-sd-name": "HP DeskJet 2700 series \\[05C516]",
    "printer-state": "idle", "printer-is-accepting-jobs": "true",
    "markers": [{"name": "tri-color ink", "level": "70",
                 "type": "ink-cartridge"},
                {"name": "black ink", "level": "40",
                 "type": "ink-cartridge"}],
}

_QUEUED = {"HP_DeskJet_2700": [
    {"job_id": "HP_DeskJet_2700-42", "user": "tony", "size": "1024",
     "when": "Mon 23 Nov 2026", "state": "pending", "origin": "cups"}]}
_HELD = {"HP_DeskJet_2700": [
    {"job_id": "HP_DeskJet_2700-42", "user": "tony", "size": "1024",
     "when": "Mon 23 Nov 2026", "state": "pending-held", "origin": "cups"}]}
_EMPTY_JOBS = {"HP_DeskJet_2700": []}


class PrinterToolTests(unittest.IsolatedAsyncioTestCase):
    """Tool-layer contract: reads free, repair confirm-gated + audited."""

    async def asyncSetUp(self):
        self.ha = AsyncMock()
        self.ha.base_url = "http://test:8123"
        self.runner = ToolRunner(self.ha, instance_id="test")
        self.runner.event_log = []

    # -- reads ---------------------------------------------------------------

    async def test_status_is_a_free_read_with_registry_verdicts(self):
        with patch("backend.printer_client.status",
                   new=AsyncMock(return_value=dict(_STATUS_PAYLOAD))) as m:
            out = await self.runner.execute("printer", {"action": "status"})
        m.assert_awaited_once_with(None)
        self.assertTrue(out["ok"])
        self.assertEqual(out["source_host"], "tony-dell")
        q = {x["name"]: x for x in out["queues"]}
        # The static queue points at the repeater — drift must name it.
        self.assertTrue(q["HP_DeskJet_2700"]["drift"]["stale_target"])
        self.assertIn("repeater", q["HP_DeskJet_2700"]["drift"]["note"])
        self.assertEqual(
            q["HP_DeskJet_2700"]["identity"]["verdict"], "unreachable")
        # The dnssd queue follows mDNS — not flagged stale.
        self.assertTrue(q["HP-2777"]["drift"]["follows_mdns"])

    async def test_status_passes_queue_filter(self):
        with patch("backend.printer_client.status",
                   new=AsyncMock(return_value=dict(_STATUS_PAYLOAD))) as m:
            await self.runner.execute(
                "printer", {"action": "status", "queue": "HP-2777"})
        m.assert_awaited_once_with("HP-2777")

    async def test_jobs_and_verify_are_free_reads(self):
        with patch("backend.printer_client.jobs",
                   new=AsyncMock(return_value={
                       "ok": True, "jobs": _QUEUED})) as mj:
            out = await self.runner.execute("printer", {"action": "jobs"})
        self.assertTrue(out["ok"])
        self.assertEqual(
            out["jobs"]["HP_DeskJet_2700"][0]["origin"], "cups")
        mj.assert_awaited_once_with(None)
        with patch("backend.printer_client.verify",
                   new=AsyncMock(return_value={
                       "ok": True, "probes": [_VERIFIED_PROBE]})) as mv:
            out = await self.runner.execute(
                "printer", {"action": "verify", "target": "192.168.2.92"})
        self.assertTrue(out["ok"])
        mv.assert_awaited_once_with(ip="192.168.2.92")

    # -- repair gating --------------------------------------------------------

    async def test_repair_denied_without_confirmation_mints_bound_token(self):
        with self.assertRaises(PermissionError) as ctx:
            await self.runner.execute(
                "printer", {"action": "repair", "op": "cancel",
                            "job_id": "HP_DeskJet_2700-42"})
        self.assertIn("confirm_token=", str(ctx.exception))
        self.assertEqual(len(self.runner._confirm_tokens), 1)

    async def test_confirmed_repair_verifies_job_acts_and_re_reads(self):
        calls = [0]
        jobs_seq = [
            {"ok": True, "jobs": dict(_QUEUED)},   # pre-read
            {"ok": True, "jobs": _EMPTY_JOBS},     # re-read after
        ]
        async def fake_jobs(queue=None):
            calls[0] += 1
            return jobs_seq[min(calls[0] - 1, 1)]
        with patch("backend.printer_client.jobs",
                   new=AsyncMock(side_effect=fake_jobs)), \
             patch("backend.printer_client.repair",
                   new=AsyncMock(
                       return_value={"ok": True, "op": "cancel-job",
                                     "job_id": "HP_DeskJet_2700-42"})) as mr:
            out = await self.runner.execute(
                "printer", {"action": "repair", "op": "cancel",
                            "job_id": "42", "confirmed": True})
        mr.assert_awaited_once_with(
            "cancel", "HP_DeskJet_2700", "HP_DeskJet_2700-42")
        self.assertTrue(out["ok"])
        self.assertEqual(out["job_id"], "HP_DeskJet_2700-42")
        self.assertEqual(out["before"][0]["job_id"], "HP_DeskJet_2700-42")
        self.assertEqual(out["after"], [])
        self.assertEqual(out["audit"]["op"], "cancel")
        self.assertEqual(out["audit"]["source_host"], "tony-dell")

    async def test_repair_refuses_job_not_in_queue(self):
        with patch("backend.printer_client.jobs",
                   new=AsyncMock(
                       return_value={"ok": True, "jobs": _EMPTY_JOBS})), \
             patch("backend.printer_client.repair",
                   new=AsyncMock()) as mr:
            out = await self.runner.execute(
                "printer", {"action": "repair", "op": "cancel",
                            "job_id": "99", "confirmed": True})
        self.assertFalse(out["ok"])
        self.assertIn("not in queue", out["error"])
        mr.assert_not_awaited()  # never acted on an unverified job

    async def test_repair_held_job_release(self):
        async def fake_jobs(queue=None):
            return {"ok": True, "jobs": dict(_HELD)}
        with patch("backend.printer_client.jobs",
                   new=AsyncMock(side_effect=fake_jobs)), \
             patch("backend.printer_client.repair",
                   new=AsyncMock(return_value={
                       "ok": True, "op": "release-job"})) as mr:
            out = await self.runner.execute(
                "printer", {"action": "repair", "op": "release",
                            "job_id": "HP_DeskJet_2700-42",
                            "confirmed": True})
        mr.assert_awaited_once_with(
            "release", "HP_DeskJet_2700", "HP_DeskJet_2700-42")
        self.assertTrue(out["ok"])

    async def test_confirm_token_binds_exact_args(self):
        # A token minted for canceling job 42 must not arm job 43.
        with self.assertRaises(PermissionError) as ctx:
            await self.runner.execute(
                "printer", {"action": "repair", "op": "cancel",
                            "job_id": "42"})
        import re
        token = re.search(r"confirm_token='([^']+)'",
                          str(ctx.exception)).group(1)
        async def fake_jobs(queue=None):
            return {"ok": True, "jobs": dict(_QUEUED)}
        with patch("backend.printer_client.jobs",
                   new=AsyncMock(side_effect=fake_jobs)), \
             patch("backend.printer_client.repair",
                   new=AsyncMock(return_value={
                       "ok": True, "op": "cancel-job"})):
            with self.assertRaises(PermissionError):
                await self.runner.execute(
                    "printer", {"action": "repair", "op": "cancel",
                                "job_id": "43", "confirm_token": token})
            out = await self.runner.execute(
                "printer", {"action": "repair", "op": "cancel",
                            "job_id": "42", "confirm_token": token})
        self.assertTrue(out["ok"])

    async def test_schema_rejects_bad_action_op_and_extra_args(self):
        for args in ({"action": "bogus"},
                     {"action": "repair", "op": "all", "job_id": "1"},
                     {"action": "repair", "op": "cancel", "job_id": "1",
                      "sudo": "rm -rf /"},
                     {"action": "repair", "op": "test-page"}):
            out = await self.runner.execute("printer", dict(args))
            self.assertFalse(out["ok"], args)
            self.assertEqual(out["error_type"], "SchemaError")

    async def test_read_only_env_blocks_repair_not_reads(self):
        with patch.dict(os.environ, {"ADA_READ_ONLY": "true"}), \
             patch("backend.printer_client.status",
                   new=AsyncMock(return_value=dict(_STATUS_PAYLOAD))):
            out = await self.runner.execute("printer", {"action": "status"})
            self.assertTrue(out["ok"])
            with self.assertRaises(PermissionError):
                await self.runner.execute(
                    "printer", {"action": "repair", "op": "cancel",
                                "job_id": "42", "confirmed": True})

    async def test_secondary_turn_denied_even_for_reads(self):
        with patch.object(ToolRunner, "_is_secondary_turn",
                          return_value=True):
            with self.assertRaises(PermissionError):
                await self.runner.execute("printer", {"action": "status"})

    async def test_repair_audit_lands_in_session_event_log(self):
        async def fake_jobs(queue=None):
            return {"ok": True, "jobs": dict(_QUEUED)}
        with patch("backend.printer_client.jobs",
                   new=AsyncMock(side_effect=fake_jobs)), \
             patch("backend.printer_client.repair",
                   new=AsyncMock(return_value={"ok": True})):
            await self.runner.execute(
                "printer", {"action": "repair", "op": "cancel",
                            "job_id": "42", "confirmed": True})
        kinds = [e["kind"] for e in self.runner.event_log]
        self.assertIn("printer_repair", kinds)


class EvaluateStatusTests(unittest.TestCase):
    """Registry cross-check — the drift verdicts the model narrates."""

    def test_verified_probe_matches_registry(self):
        payload = {"ok": True, "host": "tony-dell", "queues": [
            {"name": "HP_DeskJet_2700", "device_uri":
                "ipp://192.168.2.92/ipp/print",
             "device": dict(_VERIFIED_PROBE), "jobs": []}]}
        out = printer_client.evaluate_status(payload)
        q = out["queues"][0]
        self.assertEqual(q["identity"]["verdict"], "match")
        self.assertEqual(
            q["identity"]["evidence"]["serial"], "CN33BDZ0KS")
        self.assertFalse(q["drift"].get("stale_target", False))
        self.assertEqual(out["registry_device"], "hp-deskjet-2700")

    def test_stale_target_names_the_repeater(self):
        out = printer_client.evaluate_status(dict(_STATUS_PAYLOAD))
        q = {x["name"]: x for x in out["queues"]}["HP_DeskJet_2700"]
        self.assertTrue(q["drift"]["stale_target"])
        self.assertIn("repeater", q["drift"]["note"])
        self.assertIn("remediation", q)  # offers the verified retarget

    def test_unreachable_probe_is_not_an_identity_match(self):
        out = printer_client.evaluate_status(dict(_STATUS_PAYLOAD))
        q = {x["name"]: x for x in out["queues"]}["HP_DeskJet_2700"]
        self.assertEqual(q["identity"]["verdict"], "unreachable")

    def test_serial_mismatch_flags_the_wrong_device(self):
        bad = dict(_VERIFIED_PROBE)
        bad["serial"] = "WRONGSERIAL"
        payload = {"ok": True, "host": "x", "queues": [
            {"name": "HP_DeskJet_2700",
             "device_uri": "ipp://192.168.2.99/ipp/print",
             "device": bad, "jobs": []}]}
        out = printer_client.evaluate_status(payload)
        q = out["queues"][0]
        self.assertEqual(q["identity"]["verdict"], "mismatch")
        self.assertIn("serial", q["identity"]["mismatched"])


class HelperParsingTests(unittest.TestCase):
    """scripts/ada-cups-helper.py — offline parse + allowlist checks."""

    def test_job_id_validation(self):
        for good in ("42", "HP_DeskJet_2700-42", "HP-2777-7"):
            self.assertTrue(helper.JOB_ID_RE.match(good), good)
        for bad in ("-a", "all", "--all", "queue-abc", "1;rm -rf /",
                    "x y", ""):
            self.assertFalse(helper.JOB_ID_RE.match(bad), bad)

    def test_repair_argv_is_single_job_never_cancel_all(self):
        self.assertEqual(helper._JOB_OPS["cancel-job"]("Q-7"),
                         ["cancel", "Q-7"])
        self.assertEqual(helper._JOB_OPS["release-job"]("Q-7"),
                         ["lp", "-i", "Q-7", "-H", "resume"])
        self.assertEqual(helper._JOB_OPS["retry-job"]("Q-7"),
                         ["lp", "-i", "Q-7", "-H", "restart"])
        for fn in helper._JOB_OPS.values():
            argv = fn("Q-7")
            self.assertNotIn("-a", argv)
            self.assertNotIn("--all", argv)

    def test_lpstat_parsers(self):
        def fake_run(argv, timeout=helper.TIMEOUT_S):
            cmd = tuple(argv)
            if cmd == ("lpstat", "-v"):
                return 0, _LPSTAT_V, ""
            if cmd == ("lpstat", "-p"):
                return 0, _LPSTAT_P, ""
            if cmd == ("lpstat", "-a"):
                return 0, _LPSTAT_A, ""
            if cmd == ("lpstat", "-d"):
                return 0, _LPSTAT_D, ""
            if cmd[0] == "lpstat" and cmd[1] == "-o":
                return 0, "", ""
            return 1, "", f"unexpected {cmd}"
        with patch.object(helper, "_run", side_effect=fake_run):
            queues = helper._queues()
        by = {q["name"]: q for q in queues}
        self.assertEqual(
            by["HP_DeskJet_2700"]["target_host"], "192.168.2.75")
        self.assertEqual(by["HP_DeskJet_2700"]["state"], "idle")
        self.assertTrue(by["HP_DeskJet_2700"]["accepting"])
        self.assertTrue(by["HP_DeskJet_2700"]["is_default"])
        self.assertTrue(
            by["HP-2777"]["target_host"].endswith("._tcp.local"))

    def test_probe_attrs_extracts_identity_and_ink(self):
        def fake_ipptool(uri, request, timeout=helper.TIMEOUT_S):
            return 0, _IPP_PROBE_92, ""
        with patch.object(helper, "_ipptool", side_effect=fake_ipptool):
            probe = helper._probe_attrs("ipp://192.168.2.92/ipp/print")
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["serial"], "CN33BDZ0KS")
        self.assertEqual(
            probe["uuid"], "5ba42c21-ae68-7f6d-830e-600ee5b906fe")
        levels = {m["name"]: m["level"] for m in probe["markers"]}
        self.assertEqual(levels, {"tri-color ink": "70",
                                  "black ink": "40"})

    def test_ipp_jobs_parses_states_and_origin(self):
        text = """\
        job-id (integer) = 42
        job-name (nameWithoutLanguage) = page1
        job-state (enum) = pending-held
        job-originating-user-name (nameWithoutLanguage) = tony
        job-id (integer) = 43
        job-name (nameWithoutLanguage) = page2
        job-state (enum) = processing
"""
        with patch.object(helper, "_ipptool",
                          return_value=(0, text, "")):
            jobs = helper._ipp_jobs("ipp://localhost/printers/Q")
        self.assertEqual(jobs[0]["job_id"], "42")
        self.assertEqual(jobs[0]["state"], "pending-held")
        self.assertEqual(jobs[1]["state"], "processing")

    def _run_main(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = helper.main(["ada-cups-helper", *argv])
        return rc, json.loads(buf.getvalue())

    def test_unknown_op_and_cancel_all_rejected(self):
        rc, out = self._run_main()
        self.assertEqual(rc, 2)
        rc, out = self._run_main("shell", "rm -rf /")
        self.assertEqual(rc, 2)
        rc, out = self._run_main("cancel-all")
        self.assertEqual(rc, 2)
        rc, out = self._run_main("cancel-job", "-a")
        self.assertEqual(rc, 1)
        self.assertIn("bad job id", out["error"])
        # No such op — a test page can never be spelled.
        rc, out = self._run_main("test-page")
        self.assertEqual(rc, 2)

    def test_cancel_job_refuses_unknown_job(self):
        with patch.object(helper, "_cups_jobs",
                          return_value={"Q": []}):
            rc, out = self._run_main("cancel-job", "99")
        self.assertEqual(rc, 1)
        self.assertIn("not in any CUPS queue", out["error"])

    def test_set_queue_uri_requires_verified_identity(self):
        # No expect flags -> refuse.
        rc, out = self._run_main(
            "set-queue-uri", "HP_DeskJet_2700",
            "ipp://192.168.2.92/ipp/print")
        self.assertEqual(rc, 1)
        self.assertIn("expect", out["error"])
        # Probe mismatches the expected identity -> refuse to retarget.
        bad = dict(_VERIFIED_PROBE)
        bad["serial"] = "REPEATER"
        with patch.object(helper, "_queues", return_value=[
                {"name": "HP_DeskJet_2700"}]), \
             patch.object(helper, "_probe_attrs", return_value=bad):
            rc, out = self._run_main(
                "set-queue-uri", "HP_DeskJet_2700",
                "ipp://192.168.2.92/ipp/print",
                "--expect-serial", "CN33BDZ0KS")
        self.assertEqual(rc, 1)
        self.assertIn("identity mismatch", out["error"])
        self.assertIn("mismatched", out)
        # Verified identity -> lpadmin runs and the new uri is re-read.
        lpadmin = []
        def fake_run(argv, timeout=helper.TIMEOUT_S):
            lpadmin.append(argv)
            return 0, "", ""
        with patch.object(helper, "_queues", return_value=[
                {"name": "HP_DeskJet_2700",
                 "device_uri": "ipp://192.168.2.92/ipp/print"}]), \
             patch.object(helper, "_probe_attrs",
                          return_value=dict(_VERIFIED_PROBE)), \
             patch.object(helper, "_run", side_effect=fake_run):
            rc, out = self._run_main(
                "set-queue-uri", "HP_DeskJet_2700",
                "ipp://192.168.2.92/ipp/print",
                "--expect-serial", "CN33BDZ0KS",
                "--expect-uuid",
                "5ba42c21-ae68-7f6d-830e-600ee5b906fe")
        self.assertEqual(rc, 0)
        self.assertEqual(lpadmin[0],
                         ["lpadmin", "-p", "HP_DeskJet_2700",
                          "-v", "ipp://192.168.2.92/ipp/print"])
        self.assertEqual(out["verified"]["serial"], "CN33BDZ0KS")


class HelperStatusTests(unittest.TestCase):
    """status op end-to-end offline: idle + stale-target + queued."""

    def test_status_marks_internal_vs_cups_jobs(self):
        def fake_queues():
            return [{"name": "HP_DeskJet_2700", "state": "idle",
                     "accepting": True,
                     "device_uri": "ipp://192.168.2.92/ipp/print",
                     "target_host": "192.168.2.92"}]
        def fake_cups(queue=None):
            return {"HP_DeskJet_2700": [
                dict(j, origin="cups") for j in
                [{"job_id": "HP_DeskJet_2700-42", "state": "pending"}]]}
        def fake_device_probe(uri):
            return {"uri": uri, "target": "192.168.2.92",
                    "probed": True, "ok": True}
        def fake_ipp_jobs(uri, which="not-completed"):
            # Printer-internal job 88 never passed through CUPS.
            return [{"job_id": "HP_DeskJet_2700-42"},
                    {"job_id": "88", "state": "processing"}]
        with patch.object(helper, "_queues", side_effect=fake_queues), \
             patch.object(helper, "_cups_jobs", side_effect=fake_cups), \
             patch.object(helper, "_device_probe",
                          side_effect=fake_device_probe), \
             patch.object(helper, "_ipp_jobs",
                          side_effect=fake_ipp_jobs):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = helper.main(["ada-cups-helper", "status"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        q = out["queues"][0]
        self.assertEqual(q["jobs"][0]["origin"], "cups")
        internal = q["printer_internal_jobs"]
        self.assertEqual([j["job_id"] for j in internal], ["88"])
        self.assertEqual(internal[0]["origin"], "printer-internal")


if __name__ == "__main__":
    unittest.main()

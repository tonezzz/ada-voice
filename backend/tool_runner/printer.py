"""Printer diagnostics + bounded repair tool (ada-printer-diagnostics-
safe-repair, 2026-11-23).

status/jobs/verify are read-only; repair runs ONE allowlisted CUPS job op
(release|retry|cancel one verified job — the card's approved
confirmed-single-job-repair scope) behind the same confirm gate
(confirmed=true or a bound confirm_token, fingerprinted to exact args)
that docs action=print uses. Repair re-reads the queue after acting and
records an audit entry. Never cancel-all, never a test page, never an
arbitrary command — the print host only exposes ada-cups-helper's
allowlist anyway.
"""
from __future__ import annotations

from .common import *  # noqa: F401,F403
from backend import printer_client


class PrinterMixin:

    _PRINTER_READ_ACTIONS = frozenset({"status", "jobs", "verify"})
    # Per-job recovery only (Tony's approved scope): release a held job,
    # retry a stopped job, cancel that one job. Queue reconfiguration and
    # test prints are deliberately absent — separate approval required.
    _PRINTER_REPAIR_OPS = frozenset({"release", "retry", "cancel"})

    def _check_printer_confirmed(
        self, name: str, args: dict[str, Any], confirmed: Any,
        confirm_token: Any = None,
    ) -> None:
        """Per-action gate: reads stay free, repair needs the bound
        confirmation. Raises PermissionError on denial."""
        if str(args.get("action") or "").strip().lower() \
                in self._PRINTER_READ_ACTIONS:
            return
        if os.environ.get("ADA_READ_ONLY") == "true":
            logger.warning("denied %s %r: ADA_READ_ONLY", name, args)
            raise PermissionError(
                "printer tools are disabled (ADA_READ_ONLY=true)")
        self._require_confirmation(
            name, args, confirmed, confirm_token,
            "printer repair changes a live CUPS queue. Restate the op, "
            "queue, and job id, get an explicit yes, then call again "
            "with confirmed=true.",
        )

    async def printer(
        self, action: str, queue: str = "", job_id: str = "",
        op: str = "", target: str = "",
    ) -> Any:
        """HP DeskJet / CUPS surface: 'status' queue+device identity,
        reachability, state, accepting, ink; 'jobs' queued ids/states;
        'verify' IPP identity probe of target=; 'repair' one job op —
        confirm-gated in _execute_gated before dispatch."""
        action = (action or "").strip().lower()
        if action == "status":
            return await self._printer_status(queue or None)
        if action == "jobs":
            return await self._printer_jobs(queue or None)
        if action == "verify":
            return await self._printer_verify(target or None)
        if action == "repair":
            return await self._printer_repair(
                op=op, queue=queue or None, job_id=job_id)
        raise ValueError(
            f"invalid action {action!r}: expected status|jobs|verify|repair")

    async def _printer_status(self, queue: str | None = None) -> dict[str, Any]:
        """Queue census + device probe, annotated with registry identity/
        drift verdicts (tony-dell CUPS host via ada-cups-helper)."""
        payload = await printer_client.status(queue)
        return printer_client.evaluate_status(payload)

    async def _printer_jobs(self, queue: str | None = None) -> dict[str, Any]:
        """Queued job ids/states — origin='cups' marks jobs this host can
        repair; 'printer-internal' marks device-side jobs outside CUPS."""
        return await printer_client.jobs(queue)

    async def _printer_verify(self, target: str | None = None) -> dict[str, Any]:
        """IPP identity probe (serial/uuid/dns-sd) of a target — run
        before trusting any queue target or after suspected IP drift."""
        return await printer_client.verify(ip=target or None)

    def _resolve_job(self, queue: str, job_id: str,
                     jobs: dict[str, list[dict]]) -> str:
        """Live job id — '42' and 'queue-42' bind to the same job only
        when the queue's live list actually contains it (helpers may
        report either spelling)."""
        bare = job_id.rsplit("-", 1)[-1]
        for j in jobs.get(queue) or []:
            jid = str(j.get("job_id") or "")
            if jid == job_id or jid == bare or \
                    jid.rsplit("-", 1)[-1] == bare:
                return jid
        listed = [str(j.get("job_id")) for j in jobs.get(queue) or []]
        raise ValueError(
            f"job {job_id!r} is not in queue '{queue}' "
            f"(live ids: {listed or ['<empty>']}) — re-run "
            "action='jobs' for current state")

    async def _printer_repair(
        self, op: str, queue: str | None, job_id: str,
    ) -> dict[str, Any]:
        """One verified job op. The confirmation gate already ran (the
        token is fingerprinted to THESE args); here we verify the job is
        really in the queue, act via the helper's allowlist, re-read the
        queue, and leave an audit record."""
        op = (op or "").strip().lower()
        if op not in self._PRINTER_REPAIR_OPS:
            raise ValueError(
                f"invalid op {op!r}: expected "
                f"{'|'.join(sorted(self._PRINTER_REPAIR_OPS))} — "
                "one job, never cancel-all")
        if not str(job_id or "").strip():
            raise ValueError(
                "repair needs job_id — run action='jobs' first and pick "
                "the exact queued job")
        queue = queue or printer_client.ADA_CUPS_QUEUE
        before = await printer_client.jobs(queue)
        jid = self._resolve_job(queue, str(job_id).strip(),
                                before.get("jobs") or {})
        result = await printer_client.repair(op, queue, jid)
        after = await printer_client.jobs(queue)
        audit = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "op": op, "queue": queue, "job_id": jid,
            "source_host": after.get("host") or before.get("host")
            or printer_client.ADA_CUPS_SSH,
        }
        self._log_session_event("printer_repair", **audit)
        ok = bool(result.get("ok"))
        return {
            "ok": ok,
            "op": op, "queue": queue, "job_id": jid,
            "helper": result,
            "before": (before.get("jobs") or {}).get(queue) or [],
            "after": (after.get("jobs") or {}).get(queue) or [],
            "audit": audit,
            **({"error": result.get("error")} if not ok else {}),
        }

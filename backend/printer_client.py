"""Client for the print host's narrow CUPS helper (ada-cups-helper).

Ada never gets a shell on the print host: every call is
`ssh <host> ada-cups-helper <op> [args]` — the same lane
doc_archive_client.doc_print_pdf already uses for `lp`, narrowed to the
helper's allowlist (card ada-printer-diagnostics-safe-repair).

The helper returns one JSON object on stdout; this module parses it and
cross-checks results against backend/printer_registry.yml — the
authoritative identity map. IP addresses are observations; serial/uuid/
dns-sd-name are identity (the LAN DHCP-drifts: card intel said .78, the
live device answered .92 on 2026-11-23).

Env:
  ADA_CUPS_SSH      print host ssh alias (default DOC_PRINT_SSH/tony-dell)
  ADA_CUPS_HELPER   remote command (default
                    'python3 ~/bin/ada-cups-helper.py' — the helper ships
                    as scripts/ada-cups-helper.py and is installed under
                    the print host's home; non-interactive ssh PATH skips
                    ~/.local/bin so a full path is required)
  ADA_CUPS_QUEUE    default queue (default DOC_PRINT_QUEUE/HP_DeskJet_2700)
  ADA_PRINTER_REGISTRY  registry path override
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

ADA_CUPS_SSH = os.environ.get(
    "ADA_CUPS_SSH") or os.environ.get("DOC_PRINT_SSH", "tony-dell")
ADA_CUPS_HELPER = os.environ.get(
    "ADA_CUPS_HELPER", "python3 ~/bin/ada-cups-helper.py")
ADA_CUPS_QUEUE = os.environ.get(
    "ADA_CUPS_QUEUE") or os.environ.get("DOC_PRINT_QUEUE", "HP_DeskJet_2700")
_REGISTRY_PATH = Path(os.environ.get(
    "ADA_PRINTER_REGISTRY")
    or Path(__file__).resolve().parent / "printer_registry.yml")

_registry_cache: dict[str, Any] | None = None


def registry() -> dict[str, Any]:
    """The authoritative device/queue registry (yaml next to this file)."""
    global _registry_cache
    if _registry_cache is None:
        try:
            _registry_cache = yaml.safe_load(
                _REGISTRY_PATH.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("printer registry unreadable: %s", exc)
            _registry_cache = {}
    return _registry_cache


def _registry_queue(name: str) -> dict[str, Any]:
    for q in registry().get("queues") or []:
        if str(q.get("name") or "") == name:
            return q
    return {}


def _registry_device(name: str) -> dict[str, Any]:
    return (registry().get("devices") or {}).get(name) or {}


async def _helper(*args: str, timeout: float = 35.0) -> dict[str, Any]:
    """ssh <host> <helper> <args> -> parsed JSON. Raises RuntimeError on
    transport failure or a non-JSON answer."""
    remote = ADA_CUPS_HELPER
    if args:
        remote = f"{remote} {shlex.join([str(a) for a in args])}"
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        ADA_CUPS_SSH, remote,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    text = out.decode(errors="replace").strip()
    try:
        return json.loads(text)
    except ValueError:
        raise RuntimeError(
            f"cups helper returned non-JSON (rc={proc.returncode}): "
            f"{(err or out).decode(errors='replace').strip()[:300]}")


async def status(queue: str | None = None) -> dict[str, Any]:
    args = (["status"] + ([queue] if queue else []))
    return await _helper(*args)


async def jobs(queue: str | None = None) -> dict[str, Any]:
    args = (["jobs"] + ([queue] if queue else []))
    return await _helper(*args)


async def verify(ip: str | None = None,
                 queue: str | None = None) -> dict[str, Any]:
    args = ["verify"]
    if ip:
        args += ["--ip", ip]
    if queue:
        args += ["--queue", queue]
    return await _helper(*args)


async def repair(op: str, queue: str, job_id: str) -> dict[str, Any]:
    """One allowlisted job op over the helper. `queue` is only checked
    caller-side (job ids embed their queue); the helper re-verifies the
    job exists before acting."""
    helper_op = {"release": "release-job", "retry": "retry-job",
                 "cancel": "cancel-job"}[op]
    return await _helper(helper_op, job_id)


# ---------------------------------------------------------------- registry
# cross-check — turn the helper's raw observation into an identity/drift
# verdict a voice model can narrate.

def _identity_verdict(probe: dict[str, Any],
                      device: dict[str, Any]) -> dict[str, Any]:
    """Compare a live IPP probe with the registered identity."""
    if not probe.get("probed"):
        return {"verdict": "unprobed",
                "detail": probe.get("error") or "no probe target"}
    if not probe.get("ok"):
        return {"verdict": "unreachable",
                "detail": str(probe.get("error") or "")[:200]}
    if not device:
        return {"verdict": "unregistered",
                "detail": "device not in the registry"}
    evidence = {}
    mismatches = {}
    for key, probe_key in (("serial", "serial"), ("uuid", "uuid")):
        want = str(device.get(key) or "")
        got = str(probe.get(probe_key) or "")
        if want or got:
            evidence[key] = got
            if want and got and want != got:
                mismatches[key] = {"registered": want, "probed": got}
    dns_sd = str(probe.get("printer-dns-sd-name") or "")
    if dns_sd:
        evidence["dns_sd_name"] = dns_sd
        reg_sd = str(device.get("dns_sd_name") or "")
        if reg_sd and dns_sd.replace("\\", "") != reg_sd and \
                not dns_sd.replace("\\", "").startswith(
                    reg_sd.split("[")[0].strip()):
            mismatches["dns_sd_name"] = {
                "registered": reg_sd, "probed": dns_sd}
    if mismatches:
        return {"verdict": "mismatch", "evidence": evidence,
                "mismatched": mismatches}
    if evidence:
        return {"verdict": "match", "evidence": evidence}
    return {"verdict": "unverified",
            "detail": "probe ok but no identity attributes returned"}


def _drift_verdict(queue_name: str, target_host: str,
                   device: dict[str, Any]) -> dict[str, Any]:
    """Is the queue's device-uri host where the registered device actually
    is? stale_targets (repeater, dead leases, the old mn01 plan) are
    called out by name."""
    stale = {str(s.get("target")): str(s.get("note") or "")
             for s in (device.get("stale_targets") or [])}
    vip = str(device.get("last_verified_ip") or "")
    out: dict[str, Any] = {}
    if target_host in stale:
        out = {"stale_target": True, "target": target_host,
               "note": stale[target_host]}
    elif vip and target_host and target_host != vip \
            and not target_host.endswith(".local"):
        out = {"stale_target": True, "target": target_host,
               "note": f"queue targets {target_host} but the verified "
                       f"device ip is {vip}"}
    if target_host.endswith(".local") or target_host.endswith(
            "._tcp.local"):
        out = {"follows_mdns": True, "target": target_host,
               "note": "dnssd target — tracks the device across DHCP"}
    return out


def evaluate_status(payload: dict[str, Any],
                    reg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Annotate a helper `status` payload with registry verdicts:
    per queue — identity match/mismatch/unreachable, stale-target drift,
    a remediation hint; top-level — source host + registry device."""
    reg = reg if reg is not None else registry()
    out = dict(payload)
    out["source_host"] = payload.get("host") or ADA_CUPS_SSH
    out["registry_device"] = None
    for q in out.get("queues") or []:
        rq = _registry_queue(str(q.get("name") or ""))
        dev_name = str(rq.get("device") or "")
        device = _registry_device(dev_name)
        if dev_name and out["registry_device"] is None:
            out["registry_device"] = dev_name
        device_probe = q.get("device") or {}
        target_host = str(device_probe.get("target") or "")
        q["identity"] = _identity_verdict(device_probe, device)
        q["drift"] = _drift_verdict(
            str(q.get("name") or ""), target_host, device)
        if q["drift"].get("stale_target") and device.get(
                "last_verified_ip"):
            q["remediation"] = (
                f"retarget {q['name']} to ipp://{device['last_verified_ip']}"
                "/ipp/print after IPP identity verification (operator path "
                "— queue reconfiguration needs separate approval)")
    return out

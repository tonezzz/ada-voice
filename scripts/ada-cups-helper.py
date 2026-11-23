#!/usr/bin/env python3
"""ada-cups-helper — narrow CUPS diagnostics + bounded job repair on the
print host (card ada-printer-diagnostics-safe-repair, 2026-11-23).

Installed on the CUPS host as ~/.local/bin/ada-cups-helper; Ada reaches it
through `ssh <print-host> ada-cups-helper <op> [args]` — the SAME ssh
lane doc_print_pdf already uses, narrowed to a fixed allowlist instead of
arbitrary commands. Pin the ssh key to this binary (command= in
authorized_keys) and Ada's printer channel provably cannot do more.

Allowlist (anything else exits 2 without touching CUPS):
    status [QUEUE]              queue list + device target + jobs +
                                device identity/ink probe (read-only)
    jobs [QUEUE]                queued job ids/states for the queue
                                (or every queue) (read-only)
    verify [--ip HOST]          IPP identity probe of one target, or of
                                each queue's device host (read-only)
    release-job JOB_ID          lp -i JOB_ID -H resume   (un-hold one job)
    retry-job JOB_ID            lp -i JOB_ID -H restart  (requeue one job)
    cancel-job JOB_ID           cancel JOB_ID            (exactly one job)
    set-queue-uri QUEUE URI     lpadmin retarget — refused unless a live
        [--expect-serial S]     IPP probe of URI's host matches the
        [--expect-uuid U]       supplied identity (>=1 expect flag
                                required). Operator path; Ada's repair
                                allowlist cannot reach it.

Every reply is one JSON object on stdout — {"ok": false, "error": ...}
on failure. Job ops never accept -a/all and never print test pages.
Pure stdlib; run on the print host, tested offline by pytest via
import (see tests/test_printer.py).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

TIMEOUT_S = float(os.environ.get("ADA_CUPS_HELPER_TIMEOUT", "15"))
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*-\d+$|^\d+$")
URI_HOST_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/:?#]+)")

# IPP job-state enums -> readable state (RFC 2911/8011).
JOB_STATES = {
    "3": "pending", "4": "pending-held", "5": "processing",
    "6": "processing-stopped", "7": "canceled", "8": "aborted",
    "9": "completed",
}

# Attributes the identity probe keeps (full Get-Printer-Attributes is
# ~13KB — the status result stays model-sized by returning only these).
_PROBE_KEEP = (
    "printer-uuid", "printer-name", "printer-dns-sd-name",
    "printer-device-id", "printer-make-and-model", "printer-state",
    "printer-state-reasons", "printer-is-accepting-jobs",
    "queued-job-count", "printer-up-time",
    "marker-names", "marker-levels", "marker-types",
    "marker-low-levels", "marker-high-levels", "marker-colors",
    "printer-supply-description", "printer-info",
)

_ATTR_RE = re.compile(r"^\s*([a-zA-Z0-9-]+)\s*\([^)]+\)\s*=\s*(.*)$")
_DEVICE_SERIAL_RE = re.compile(r"(?:^|;)SN:([^;]+)")


def _run(argv: list[str], timeout: float = TIMEOUT_S) -> tuple[int, str, str]:
    """argv-exec only — never a shell. Returns (rc, out, err)."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not installed on this host"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s: {' '.join(argv[:3])}"


def _fail(msg: str, **extra) -> int:
    print(json.dumps({"ok": False, "error": msg, **extra}))
    return 1


# ---------------------------------------------------------------- lpstat

_LPSTAT_V_RE = re.compile(r"^device for (\S+):\s+(\S+)")
_LPSTAT_P_RE = re.compile(r"^printer (\S+)\s+(.*)$")
_LPSTAT_A_RE = re.compile(r"^(\S+) accepting requests since\s+(.*)$")
_LPSTAT_O_RE = re.compile(r"^(\S+)-(\d+)\s+(\S+)\s+(\d+)\s+(.*)$")


def _uri_host(uri: str) -> str:
    """Host part of a device-uri — ipp(s)://1.2.3.4/... -> 1.2.3.4;
    dnssd://x._ipp._tcp.local/... -> the .local name; '' otherwise."""
    m = URI_HOST_RE.match(str(uri or ""))
    return m.group(1) if m else ""


def _queues() -> list[dict]:
    """CUPS queue census: name, device_uri + target host, state,
    accepting flag, default marker."""
    out: dict[str, dict] = {}
    rc, stdout, _ = _run(["lpstat", "-v"])
    for line in stdout.splitlines():
        m = _LPSTAT_V_RE.match(line.strip())
        if m:
            q, uri = m.group(1), m.group(2)
            out[q] = {"name": q, "device_uri": uri,
                      "target_host": _uri_host(uri)}
    rc, stdout, _ = _run(["lpstat", "-p"])
    for line in stdout.splitlines():
        m = _LPSTAT_P_RE.match(line.strip())
        if not m or m.group(1) not in out:
            continue
        rest = m.group(2)
        state = "unknown"
        if rest.startswith("is idle"):
            state = "idle"
        elif rest.startswith("now printing"):
            state = "processing"
            out[m.group(1)]["printing_job"] = rest.split()[2]
        elif rest.startswith("disabled"):
            state = "stopped"
        out[m.group(1)]["state"] = state
    rc, stdout, _ = _run(["lpstat", "-a"])
    for line in stdout.splitlines():
        m = _LPSTAT_A_RE.match(line.strip())
        if m and m.group(1) in out:
            out[m.group(1)]["accepting"] = True
            out[m.group(1)]["accepting_since"] = m.group(2)
    for q in out.values():
        q.setdefault("state", "unknown")
        q.setdefault("accepting", False)
    rc, stdout, _ = _run(["lpstat", "-d"])
    m = re.search(r"system default destination:\s*(\S+)", stdout)
    if m and m.group(1) in out:
        out[m.group(1)]["is_default"] = True
    return list(out.values())


# ---------------------------------------------------------------- IPP

def _ipp_request(ops: str) -> str:
    return "{\n" + ops + "\n}\n"


def _ipptool(uri: str, request: str,
             timeout: float = TIMEOUT_S) -> tuple[int, str, str]:
    """Run ipptool -tv against `uri` with a generated request file."""
    with tempfile.NamedTemporaryFile(
            "w", suffix=".test", delete=False) as fh:
        fh.write(request)
        path = fh.name
    try:
        return _run(["ipptool", "-tv", uri, path], timeout=timeout)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _parse_attrs(text: str) -> dict[str, str]:
    """ipptool -tv attribute lines -> {name: raw-value} (last wins;
    1setOf values keep the full csv-ish raw string)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _ATTR_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _probe_attrs(uri: str) -> dict:
    """Get-Printer-Attributes on `uri` -> compact identity/state dict."""
    req = _ipp_request(
        '  OPERATION Get-Printer-Attributes\n'
        '  GROUP operation-attributes-tag\n'
        '    ATTR charset attributes-charset utf-8\n'
        '    ATTR naturalLanguage attributes-natural-language en\n'
        '    ATTR uri printer-uri ' + uri)
    rc, stdout, stderr = _ipptool(uri, req)
    if rc != 0:
        return {"ok": False, "error": (stderr or stdout).strip()[:300],
                "rc": rc, "uri": uri}
    attrs = _parse_attrs(stdout)
    if "successful-ok" not in stdout and not attrs:
        return {"ok": False, "error": "probe returned no attributes",
                "rc": rc, "uri": uri}
    out = {k: v for k, v in attrs.items() if k in _PROBE_KEEP}
    dev_id = attrs.get("printer-device-id") or ""
    m = _DEVICE_SERIAL_RE.search(dev_id)
    if m:
        out["serial"] = m.group(1)
    uuid = attrs.get("printer-uuid") or ""
    if uuid:
        out["uuid"] = uuid.replace("urn:uuid:", "")
    # Ink levels as [{name, level, type}] pairs for the model.
    names = (attrs.get("marker-names") or "").split(",")
    levels = (attrs.get("marker-levels") or "").split(",")
    types = (attrs.get("marker-types") or "").split(",")
    out["markers"] = [
        {"name": n.strip(), "level": levels[i].strip(),
         "type": types[i].strip() if i < len(types) else "unknown"}
        for i, n in enumerate(names) if n.strip() and i < len(levels)]
    out.update({"ok": True, "uri": uri})
    return out


def _ipp_jobs(uri: str, which: str = "not-completed") -> list[dict]:
    """Get-Jobs on `uri` -> [{job_id, name, state, user}] parsed from
    ipptool -tv. [] on transport failure (caller marks unreachable)."""
    req = _ipp_request(
        '  OPERATION Get-Jobs\n'
        '  GROUP operation-attributes-tag\n'
        '    ATTR charset attributes-charset utf-8\n'
        '    ATTR naturalLanguage attributes-natural-language en\n'
        '    ATTR uri printer-uri ' + uri + '\n'
        '    ATTR keyword which-jobs ' + which + '\n'
        '    ATTR keyword requested-attributes '
        'job-id,job-name,job-state,job-originating-user-name')
    rc, stdout, _ = _ipptool(uri, req)
    if rc != 0:
        return []
    jobs: list[dict] = []
    cur: dict | None = None
    for line in stdout.splitlines():
        m = _ATTR_RE.match(line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k == "job-id":
            cur = {"job_id": v}
            jobs.append(cur)
        elif cur is not None:
            if k == "job-name":
                cur["name"] = v
            elif k == "job-state":
                cur["state"] = JOB_STATES.get(v, v)
            elif k == "job-originating-user-name":
                cur["user"] = v
    return jobs


def _cups_jobs(queue: str | None = None) -> dict[str, list[dict]]:
    """Jobs on the LOCAL CUPS host per queue. Prefers Get-Jobs on
    ipp://localhost (real job-state); falls back to `lpstat -o`
    (id/user/size, state 'queued')."""
    queues = [queue] if queue else [q["name"] for q in _queues()]
    out: dict[str, list[dict]] = {}
    for q in queues:
        uri = f"ipp://localhost:631/printers/{q}"
        jobs = _ipp_jobs(uri)
        if not jobs:
            rc, stdout, _ = _run(["lpstat", "-o", q])
            for line in stdout.splitlines():
                m = _LPSTAT_O_RE.match(line.strip())
                if m and m.group(1) == q:
                    jobs.append({
                        "job_id": f"{m.group(1)}-{m.group(2)}",
                        "user": m.group(3),
                        "size": m.group(4),
                        "when": m.group(5).strip(),
                        "state": "queued"})
        for j in jobs:
            j["origin"] = "cups"
        out[q] = jobs
    return out


def _resolve_mdns(name: str) -> str:
    """`.local` / dns-sd host -> IPv4 via avahi-resolve ('' on failure)."""
    if not name.endswith(".local"):
        return name
    rc, stdout, _ = _run(["avahi-resolve", "-n", name], timeout=8)
    if rc == 0:
        parts = stdout.split()
        if len(parts) >= 2 and re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[-1]):
            return parts[-1]
    return ""


_AVAHI_ESC_RE = re.compile(r"\\(\d{3})")


def _avahi_unescape(text: str) -> str:
    return _AVAHI_ESC_RE.sub(lambda m: chr(int(m.group(1))), text)


def _dnssd_instance(uri: str) -> str:
    """dnssd://<instance>._ipp._tcp.local[/...] -> percent-decoded
    instance label ('HP DeskJet 2700 series [05C516]')."""
    host = _uri_host(uri)
    host = re.sub(r"%([0-9A-Fa-f]{2})",
                  lambda m: chr(int(m.group(1), 16)), host)
    return re.sub(r"\._[a-z]+\._tcp\.local$", "", host)


def _resolve_dnssd_service(instance: str) -> tuple[str, str]:
    """dns-sd instance label -> (hostname, ipv4) via avahi-browse.
    ('', '') when unresolved. A browse is bounded by -t (~exit after
    the cache is read)."""
    if not instance:
        return "", ""
    rc, stdout, _ = _run(
        ["avahi-browse", "-rtp", "_ipp._tcp"], timeout=15)
    for line in stdout.splitlines():
        if not line.startswith("="):
            continue
        parts = line.split(";")
        if len(parts) < 8 or parts[2] != "IPv4":
            continue
        if _avahi_unescape(parts[3]) == instance:
            return parts[6], parts[7]
    return "", ""


def _device_probe(uri: str) -> dict:
    """Probe the device a queue points at: ipp(s)://host -> IPP
    attributes; dnssd://x._ipp._tcp.local -> avahi-browse resolve, then
    probe the resolved address (dnssd targets follow the device across
    DHCP — the queue itself never goes stale)."""
    host = _uri_host(uri)
    probe = {"uri": uri, "target": host, "probed": False}
    if not host:
        probe["error"] = "unparseable device-uri"
        return probe
    if uri.startswith("dnssd://"):
        instance = _dnssd_instance(uri)
        probe["dnssd_instance"] = instance
        hostname, ip = _resolve_dnssd_service(instance)
        if not ip:
            probe["error"] = (
                f"dnssd instance {instance!r} unresolved — the device "
                "is off or off-network")
            return probe
        probe["resolved_host"] = hostname
        probe["resolved_ip"] = ip
        target_ip = ip
    else:
        target_ip = (_resolve_mdns(host)
                     if host.endswith(".local") else host)
        if host.endswith(".local") and target_ip:
            probe["resolved_ip"] = target_ip
        if not target_ip:
            probe["error"] = f"unresolvable device host {host!r}"
            return probe
    probe["probed"] = True
    probe.update(_probe_attrs(f"ipp://{target_ip}/ipp/print"))
    return probe


# ---------------------------------------------------------------- ops

def _op_status(queue: str | None) -> int:
    queues = _queues()
    if queue:
        queues = [q for q in queues if q["name"] == queue]
        if not queues:
            return _fail(f"unknown queue '{queue}'",
                         queues=[q["name"] for q in _queues()])
    jobs = _cups_jobs(queue)
    cups_ids = {j["job_id"] for lst in jobs.values() for j in lst}
    for q in queues:
        q["jobs"] = jobs.get(q["name"], [])
        probe = _device_probe(q.get("device_uri") or "")
        q["device"] = probe
        # Direct/IPP jobs sitting on the printer that never passed
        # through this CUPS host — the "internal" lane a user should
        # not confuse with the queue Ada can repair.
        q["printer_internal_jobs"] = [
            dict(j, origin="printer-internal")
            for j in (_ipp_jobs(f"ipp://{probe['resolved_ip'] if probe.get('resolved_ip') else probe.get('target')}/ipp/print")
                      if probe.get("ok") else [])
            if j.get("job_id") not in cups_ids]
    return _ok(op="status", host=_hostname(), queues=queues)


def _op_jobs(queue: str | None) -> int:
    return _ok(op="jobs", host=_hostname(), jobs=_cups_jobs(queue))


def _op_verify(ip: str | None, queue: str | None) -> int:
    targets: list[str] = []
    if ip:
        targets = [ip]
    elif queue:
        qs = [q for q in _queues() if q["name"] == queue]
        if not qs:
            return _fail(f"unknown queue '{queue}'")
        host = _uri_host(qs[0].get("device_uri") or "")
        if host:
            targets = [host]
    else:
        seen = set()
        for q in _queues():
            host = _uri_host(q.get("device_uri") or "")
            if host and host not in seen:
                seen.add(host)
                targets.append(host)
    if not targets:
        return _fail("no target — pass --ip HOST or --queue NAME")
    probes = []
    for t in targets:
        ip_t = _resolve_mdns(t) if t.endswith(".local") else t
        probe = _probe_attrs(f"ipp://{ip_t or t}/ipp/print")
        probe["target"] = t
        probes.append(probe)
    return _ok(op="verify", host=_hostname(), probes=probes)


_JOB_OPS = {
    # release a held job / retry a stopped job / cancel exactly one job —
    # the per-job recovery surface approved on the card (never a queue
    # flag, never -a, never an arbitrary command).
    "release-job": lambda jid: ["lp", "-i", jid, "-H", "resume"],
    "retry-job": lambda jid: ["lp", "-i", jid, "-H", "restart"],
    "cancel-job": lambda jid: ["cancel", jid],
}


def _job_known(jid: str) -> str | None:
    """Queue the job currently sits in ('' = listed nowhere → the
    caller treats it as unverifiable), None = lookup failed.
    '42' and 'queue-42' resolve the same way — lpstat/ipptool report
    either spelling."""
    bare = jid.rsplit("-", 1)[-1]
    try:
        for q, lst in _cups_jobs().items():
            for j in lst:
                listed = str(j.get("job_id") or "")
                if listed in (jid, bare) or \
                        listed.rsplit("-", 1)[-1] == bare:
                    return q
    except Exception:
        return None
    return ""


def _op_job(op: str, jid: str) -> int:
    if not JOB_ID_RE.match(jid):
        return _fail(f"bad job id {jid!r} — expected <queue>-<n> or <n>")
    q = _job_known(jid)
    if q == "":
        return _fail(
            f"job {jid!r} is not in any CUPS queue — nothing to {op}; "
            "re-run 'status' for the live job list")
    rc, stdout, stderr = _run(_JOB_OPS[op](jid))
    if rc != 0:
        return _fail(
            f"{op} {jid}: rc={rc} {(stderr or stdout).strip()[:200]}")
    return _ok(op=op, job_id=jid, queue=q,
               output=(stdout or stderr).strip()[:200])


def _op_set_queue_uri(queue: str, uri: str, expect_serial: str,
                      expect_uuid: str) -> int:
    """Privileged retarget — refuses to run on a device that has not
    re-verified its identity THIS call (the .75 repeater failure mode:
    a queue that silently points at whatever answered the IP)."""
    if not (expect_serial or expect_uuid):
        return _fail(
            "set-queue-uri requires --expect-serial and/or --expect-uuid "
            "— identity must be verified, never assumed")
    if queue not in {q["name"] for q in _queues()}:
        return _fail(f"unknown queue '{queue}'")
    host = _uri_host(uri)
    if not host:
        return _fail(f"unparseable uri {uri!r} — expected ipp(s):// or dnssd://")
    ip = _resolve_mdns(host) if host.endswith(".local") else host
    probe = _probe_attrs(f"ipp://{ip or host}/ipp/print")
    if not probe.get("ok"):
        return _fail(
            f"identity probe of {host} failed: {probe.get('error')}",
            probe=probe)
    mismatched = {}
    if expect_serial and probe.get("serial") != expect_serial:
        mismatched["serial"] = {
            "expect": expect_serial, "got": probe.get("serial")}
    if expect_uuid and str(probe.get("uuid") or "") != expect_uuid.replace(
            "urn:uuid:", ""):
        mismatched["uuid"] = {
            "expect": expect_uuid, "got": probe.get("uuid")}
    if mismatched:
        return _fail(
            f"identity mismatch at {host} — NOT retargeting "
            f"'{queue}' (this is exactly the stale-target class the "
            "helper exists to stop)", mismatched=mismatched, probe=probe)
    rc, stdout, stderr = _run(
        ["lpadmin", "-p", queue, "-v", uri], timeout=TIMEOUT_S)
    if rc != 0:
        return _fail(
            f"lpadmin rc={rc}: {(stderr or stdout).strip()[:200]}")
    new_uri = next(
        (q["device_uri"] for q in _queues() if q["name"] == queue), "")
    return _ok(op="set-queue-uri", queue=queue, uri=uri,
               verified={k: probe.get(k) for k in
                         ("serial", "uuid", "printer-name",
                          "printer-dns-sd-name")},
               device_uri_now=new_uri)


def _hostname() -> str:
    try:
        return os.uname().nodename
    except OSError:
        return "unknown"


def _ok(**fields) -> int:
    print(json.dumps({"ok": True, **fields}))
    return 0


def _usage() -> int:
    print(json.dumps({
        "ok": False,
        "error": "unknown op",
        "ops": ["status [QUEUE]", "jobs [QUEUE]",
                "verify [--ip HOST] [--queue NAME]",
                "release-job JOB_ID", "retry-job JOB_ID",
                "cancel-job JOB_ID",
                "set-queue-uri QUEUE URI "
                "[--expect-serial S] [--expect-uuid U]"],
    }))
    return 2


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    if not args:
        return _usage()
    op = args.pop(0)
    opts = {}
    pos = []
    it = iter(range(len(args)))
    skip = False
    for i in it:
        if skip:
            skip = False
            continue
        a = args[i]
        if a.startswith("--") and i + 1 < len(args):
            opts[a[2:]] = args[i + 1]
            skip = True
        else:
            pos.append(a)
    if op == "status":
        return _op_status(pos[0] if pos else None)
    if op == "jobs":
        return _op_jobs(pos[0] if pos else None)
    if op == "verify":
        return _op_verify(opts.get("ip"), opts.get("queue"))
    if op in _JOB_OPS:
        if not pos:
            return _fail(f"{op} needs a job id")
        return _op_job(op, pos[0])
    if op == "set-queue-uri":
        if len(pos) < 2:
            return _fail("set-queue-uri needs QUEUE and URI")
        return _op_set_queue_uri(
            pos[0], pos[1], opts.get("expect-serial") or "",
            opts.get("expect-uuid") or "")
    return _usage()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

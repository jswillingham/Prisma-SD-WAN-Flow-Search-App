"""
Prisma SD-WAN Flow Search — standalone Flask app.

All Prisma API calls go through the official `prisma_sase` SDK, server-side
(no CORS exposure). Each browser session gets its own SDK connection object,
kept in a server-side registry (`_connections`) keyed by an opaque id stored
in the Flask session cookie — the cookie itself can't hold a live Python
object, and this also means credentials are never persisted to disk unless
prismasase_settings.py is present (see prismasase_settings.py.example).
One connection per browser session; no multi-tenant switching yet.

Run:
    pip install -r requirements.txt
    python app.py

Open http://localhost:5050
"""

import contextlib
import io
import ipaddress
import os
import re
import runpy
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import prisma_sase
from flask import Flask, jsonify, render_template, request, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32))

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prismasase_settings.py")

# Server-side registry of live SDK connections, keyed by an opaque per-browser-
# session id (the Flask cookie session holds only this key, never the object).
_connections = {}
_connections_lock = threading.Lock()

# Server-side registry of in-progress/completed search jobs, keyed by job id.
# /api/search starts a background thread and returns immediately; the browser
# polls /api/search/status/<job_id> for live per-site completion so the UI can
# show real progress instead of one long blocking wait.
_search_jobs = {}
_search_jobs_lock = threading.Lock()
_SEARCH_JOB_TTL = 600  # seconds; evicts abandoned jobs nobody ever polled to completion


def _prune_search_jobs():
    cutoff = time.time() - _SEARCH_JOB_TTL
    with _search_jobs_lock:
        stale = [jid for jid, job in _search_jobs.items() if job["created_at"] < cutoff]
        for jid in stale:
            del _search_jobs[jid]


# ── Optional saved credentials ────────────────────────────────────────────────
# This app never writes prismasase_settings.py itself. A user who wants a
# persistent connection creates it by hand (see prismasase_settings.py.example);
# if it's present we offer a "Quick Connect" that reads it server-side only —
# the values in it are never sent back to the browser.

def _load_saved_settings():
    if not os.path.isfile(SETTINGS_FILE):
        return None
    try:
        ns = runpy.run_path(SETTINGS_FILE)
    except Exception:
        return None
    tsg_id = ns.get("PRISMASASE_TSG_ID", "")
    client_id = ns.get("PRISMASASE_CLIENT_ID", "")
    client_secret = ns.get("PRISMASASE_CLIENT_SECRET", "")
    if not all([tsg_id, client_id, client_secret]):
        return None
    return {
        "tenant_name": ns.get("PRISMASASE_TENANT_NAME", ""),
        "tsg_id": tsg_id,
        "client_id": client_id,
        "client_secret": client_secret,
    }


# ── Connection registry ───────────────────────────────────────────────────────

def _get_api():
    """Return the live prisma_sase.API for the current browser session, or None."""
    conn_id = session.get("conn_id")
    if not conn_id:
        return None
    with _connections_lock:
        return _connections.get(conn_id)


def _clear_connection():
    conn_id = session.get("conn_id")
    if conn_id:
        with _connections_lock:
            _connections.pop(conn_id, None)
    session.clear()


def _perform_connect(tsg_id, client_id, client_secret, tenant_name_override):
    """Authenticate via the prisma_sase SDK and register the connection for this
    browser session. Returns (error_string_or_None, resolved_tenant_name)."""
    if not all([tsg_id, client_id, client_secret]):
        return "All fields are required.", None

    api = prisma_sase.API()
    api.modify_rest_retry()  # enable built-in retry/backoff on 429/502/503/504

    # login_secret() reports failures via print() rather than a return value;
    # capture that output so we can relay something useful to the browser.
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        ok = api.interactive.login_secret(client_id=client_id, client_secret=client_secret, tsg_id=tsg_id)

    if not ok:
        lines = [ln.strip() for ln in captured.getvalue().splitlines() if ln.strip()]
        message = lines[-1] if lines else "Authentication failed. Check TSG ID, Client ID, and Client Secret."
        return message, None

    tenant_name = tenant_name_override or api.tenant_name or ""

    conn_id = secrets.token_hex(16)
    with _connections_lock:
        _connections[conn_id] = api

    session.clear()
    session["conn_id"] = conn_id
    session["tsg_id"] = tsg_id
    session["tenant_name"] = tenant_name
    return None, tenant_name


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    saved = _load_saved_settings()
    saved_label = ""
    if saved:
        saved_label = saved["tenant_name"] or saved["tsg_id"]
    return render_template("index.html",
                           connected=_get_api() is not None,
                           tsg_id=session.get("tsg_id", ""),
                           tenant_name=session.get("tenant_name", ""),
                           has_saved_tenant=bool(saved),
                           saved_tenant_label=saved_label)


@app.route("/api/connect", methods=["POST"])
def api_connect():
    data = request.get_json(force=True) or {}
    tsg_id      = data.get("tsg_id", "").strip()
    client_id   = data.get("client_id", "").strip()
    client_secret = data.get("client_secret", "")
    tenant_name = data.get("tenant_name", "").strip()

    _clear_connection()
    error, resolved_name = _perform_connect(tsg_id, client_id, client_secret, tenant_name)
    if error:
        status = 400 if error == "All fields are required." else 401
        return jsonify({"error": error}), status
    return jsonify({"ok": True, "tenant_name": resolved_name})


@app.route("/api/connect-saved", methods=["POST"])
def api_connect_saved():
    saved = _load_saved_settings()
    if not saved:
        return jsonify({"error": "No saved credentials found. Create prismasase_settings.py to enable this."}), 404

    _clear_connection()
    error, resolved_name = _perform_connect(saved["tsg_id"], saved["client_id"], saved["client_secret"], saved["tenant_name"])
    if error:
        return jsonify({"error": error}), 401
    return jsonify({"ok": True, "tsg_id": saved["tsg_id"], "tenant_name": resolved_name})


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    _clear_connection()
    return jsonify({"ok": True})


@app.route("/api/sites")
def api_sites():
    api = _get_api()
    if not api:
        return jsonify({"error": "Session expired — please reconnect."}), 401
    try:
        resp = api.get.sites()
        if not resp.sdk_status:
            return jsonify({"error": "Failed to load sites."}), 502
        items = resp.sdk_content.get("items") or []
        sites = sorted(
            [{"id": s["id"], "name": s["name"]}
             for s in items
             if s.get("admin_state") == "active" and s.get("element_cluster_role") != "HUB"],
            key=lambda s: s["name"],
        )
        return jsonify({"sites": sites})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/appdefs")
def api_appdefs():
    api = _get_api()
    if not api:
        return jsonify({"error": "Session expired — please reconnect."}), 401
    try:
        resp = api.get.appdefs()
        if not resp.sdk_status:
            return jsonify({"error": "Failed to load applications."}), 502
        items = resp.sdk_content.get("items") or []
        appdefs = sorted(
            [{"id": a["id"], "name": a.get("display_name") or a.get("app_name") or str(a["id"])}
             for a in items],
            key=lambda a: a["name"],
        )
        return jsonify({"appdefs": appdefs})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/policies")
def api_policies():
    """Build id -> name maps for the three policy types stamped on flow records:
    network (path) policy, priority (QoS) policy, and security (NGFW) policy.
    Each policy type is a set of rule-sets, each containing rules, so this
    requires enumerating every rule-set's rules — done in parallel with the
    same rate-limit pattern used for flow search (semaphore=2, 300ms stagger)."""
    api = _get_api()
    if not api:
        return jsonify({"error": "Session expired — please reconnect."}), 401

    def build_map(sets_resp, rules_fn):
        result = {}
        if not sets_resp.sdk_status:
            return result
        policy_sets = sets_resp.sdk_content.get("items") or []
        semaphore = threading.Semaphore(2)

        def fetch_rules(policyset_id):
            with semaphore:
                time.sleep(0.3)
                rr = rules_fn(policyset_id)
                return rr.sdk_content.get("items") or [] if rr.sdk_status else []

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(fetch_rules, s["id"]) for s in policy_sets]
            for future in as_completed(futures):
                for rule in future.result():
                    if rule.get("id"):
                        result[rule["id"]] = rule.get("name") or rule["id"]
        return result

    try:
        network_map = build_map(api.get.networkpolicysets(), api.get.networkpolicyrules)
        priority_map = build_map(api.get.prioritypolicysets(), api.get.prioritypolicyrules)
        security_map = build_map(api.get.ngfwsecuritypolicysets(), api.get.ngfwsecuritypolicyrules)
        return jsonify({"network": network_map, "priority": priority_map, "security": security_map})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/topology", methods=["POST"])
def api_topology():
    api = _get_api()
    if not api:
        return jsonify({"error": "Session expired — please reconnect."}), 401
    try:
        resp = api.post.topology({"type": "anynet", "servicelinks": True, "stub_links": True})
        if not resp.sdk_status:
            return jsonify({"error": "Failed to load topology."}), 502

        topo = {}
        for link in (resp.sdk_content.get("links") or []):
            path_id = link.get("path_id")
            if not path_id:
                continue

            link_type = link.get("type")
            if link_type == "internet-stub":
                network = link.get("network", "")
                circuit = link.get("target_circuit_name", "")
                if circuit:
                    name = f"Direct Internet ({circuit})"
                elif network:
                    name = f"Direct Internet ({network})"
                else:
                    name = "Direct Internet"
            elif link_type == "servicelink":
                sep_name = link.get("sep_name", "Unknown")
                wan_name = link.get("wan_nw_name", "")
                name = f"{sep_name} ({wan_name})" if wan_name else sep_name
            else:
                source = link.get("source_site_name", "Unknown")
                target = link.get("target_site_name", "Unknown")
                name = f"{source} → {target}"

            topo[path_id] = name

        return jsonify(topo)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# monitor_flows silently clamps any window longer than 1h to its final hour
# (verified live, regardless of result count or view.summary), so longer ranges
# are split into hourly windows and queried separately. Each query takes ~8s and
# only 2 can run concurrently, so the total range is capped.
_WINDOW = timedelta(hours=1)
_MAX_RANGE = timedelta(hours=4)
_MAX_PAGE_SIZE = 1000  # API rejects page_size > 1000 with SCHEMA_MISMATCH


def _parse_iso(value):
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _format_iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _split_windows(start, end):
    windows = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + _WINDOW, end)
        windows.append((_format_iso(cursor), _format_iso(window_end)))
        cursor = window_end
    return windows


_MAX_IPS = 10  # API rejects >10 entries per source_ip/destination_ip list (SCHEMA_MISMATCH)


def _parse_ip_list(raw):
    """Split a comma/space/newline-separated list of IPs and CIDRs. The API ORs
    multiple values within a source_ip/destination_ip filter, ANDs the two
    fields together, and accepts CIDRs (all verified live).
    Returns (normalized_list, invalid_entries)."""
    ips, invalid = [], []
    for entry in re.split(r"[\s,;]+", raw.strip()):
        if not entry:
            continue
        try:
            value = str(ipaddress.ip_network(entry, strict=False)) if "/" in entry else str(ipaddress.ip_address(entry))
        except ValueError:
            invalid.append(entry)
            continue
        if value not in ips:
            ips.append(value)
    return ips, invalid


def _flow_key(flow):
    # A long-lived flow shows up in every hourly window it overlaps.
    return (flow.get("flow_id"), flow.get("flow_start_time_ms"), flow.get("source_ip"),
            flow.get("source_port"), flow.get("destination_ip"), flow.get("destination_port"))


def _merge_site_windows(window_results, windows, page_size):
    """Combine a site's per-window results into one, newest window first to
    match the API's newest-first ordering within a window."""
    merged, errors, truncated = {}, [], []
    for idx in reversed(range(len(windows))):
        res = window_results[idx]
        if res["error"]:
            errors.append(f"{windows[idx][0]}: {res['error']}")
            continue
        if len(res["flows"]) >= page_size:
            truncated.append(windows[idx][0])
        for flow in res["flows"]:
            merged.setdefault(_flow_key(flow), flow)
    return {
        "flows": list(merged.values()),
        "error": "; ".join(errors) if errors else None,
        "truncated_windows": sorted(truncated),
    }


def _run_search_job(job_id, api, site_ids, windows, app_ids, flow_filter, page_size):
    semaphore = threading.Semaphore(2)

    def search_one(site_id, window_idx):
        start_time, end_time = windows[window_idx]
        filter_payload = {"site": [site_id]}
        if app_ids:
            filter_payload["app"] = app_ids
        if flow_filter:
            filter_payload["flow"] = flow_filter

        payload = {
            "start_time": start_time,
            "end_time": end_time,
            "debug_level": "all",
            "page_size": page_size,
            "dest_page": 1,
            "view": {"summary": False},
            "filter": filter_payload,
        }

        with semaphore:
            time.sleep(0.3)
            try:
                # SDK's built-in retry (enabled in _perform_connect) handles 429/502/503/504.
                # api_version pinned to v3.10: v3.11+ caps historical lookback at 90 days,
                # v3.10 allows well over a year. Verified live — don't bump without re-checking.
                resp = api.post.monitor_flows(payload, api_version="v3.10")
                if not resp.sdk_status:
                    result = {"flows": [], "error": str(resp.sdk_content)}
                else:
                    flows_data = (resp.sdk_content.get("flows") or {})
                    result = {"flows": flows_data.get("items") or [], "error": None}
            except Exception as e:
                result = {"flows": [], "error": str(e)}

        with _search_jobs_lock:
            job = _search_jobs.get(job_id)
            if job is None:
                return  # job was pruned/abandoned; nothing to update
            job["completed_queries"] += 1
            site_windows = job["pending_windows"].setdefault(site_id, {})
            site_windows[window_idx] = result
            if len(site_windows) == len(windows):
                job["flows_by_site"][site_id] = _merge_site_windows(job["pending_windows"].pop(site_id), windows, page_size)
                job["completed_site_ids"].append(site_id)

    # Site-major order so each site finishes (and its progress chip turns green)
    # as early as possible rather than every site finishing together at the end.
    units = [(site_id, idx) for site_id in site_ids for idx in range(len(windows))]
    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(lambda unit: search_one(*unit), units))

    with _search_jobs_lock:
        job = _search_jobs.get(job_id)
        if job is not None:
            job["done"] = True


@app.route("/api/search", methods=["POST"])
def api_search():
    api = _get_api()
    if not api:
        return jsonify({"error": "Session expired — please reconnect."}), 401

    data = request.get_json(force=True) or {}
    site_ids   = data.get("site_ids", [])
    start_time = data.get("start_time", "")
    end_time   = data.get("end_time", "")
    app_ids    = data.get("app_ids", [])
    source_raw = data.get("source_ips", "")
    dest_raw   = data.get("dest_ips", "")
    page_size  = min(int(data.get("page_size", 100)), _MAX_PAGE_SIZE)

    if not site_ids:
        return jsonify({"error": "No sites selected."}), 400
    if not start_time or not end_time:
        return jsonify({"error": "Start and end time are required."}), 400
    try:
        start, end = _parse_iso(start_time), _parse_iso(end_time)
    except ValueError:
        return jsonify({"error": "Invalid start or end time."}), 400
    if end <= start:
        return jsonify({"error": "End time must be after start time."}), 400
    if end - start > _MAX_RANGE:
        return jsonify({"error": "Time range is limited to 4 hours."}), 400

    flow_filter = {}
    for field, label, raw in (("source_ip", "Source", source_raw), ("destination_ip", "Destination", dest_raw)):
        ips, invalid = _parse_ip_list(raw)
        if invalid:
            return jsonify({"error": f"Invalid {label.lower()} IP address or CIDR: " + ", ".join(invalid)}), 400
        if len(ips) > _MAX_IPS:
            return jsonify({"error": f"{label}: up to {_MAX_IPS} IP addresses/CIDRs per search."}), 400
        if ips:
            flow_filter[field] = ips

    windows = _split_windows(start, end)
    total_queries = len(site_ids) * len(windows)

    _prune_search_jobs()

    job_id = secrets.token_hex(16)
    with _search_jobs_lock:
        _search_jobs[job_id] = {
            "total": len(site_ids),
            "total_queries": total_queries,
            "completed_queries": 0,
            "completed_site_ids": [],
            "pending_windows": {},
            "flows_by_site": {},
            "done": False,
            "created_at": time.time(),
        }

    thread = threading.Thread(
        target=_run_search_job,
        args=(job_id, api, site_ids, windows, app_ids, flow_filter, page_size),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id, "total": len(site_ids), "total_queries": total_queries, "windows": windows,
                    "flow_filter": flow_filter})


@app.route("/api/search/status/<job_id>")
def api_search_status(job_id):
    with _search_jobs_lock:
        job = _search_jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Unknown or expired search job."}), 404
        done = job["done"]
        response = {
            "total": job["total"],
            "total_queries": job["total_queries"],
            "completed_queries": job["completed_queries"],
            "completed_site_ids": list(job["completed_site_ids"]),
            "done": done,
        }
        if done:
            response["flows_by_site"] = job["flows_by_site"]
            del _search_jobs[job_id]

    return jsonify(response)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=False)

"""Flask dashboard: browse the local DB and manage items (CRUD)."""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template, request,
                   session, url_for)

from ..config import masked, target_profit_for
from ..db import STATUSES, Database, iso, parse_iso
from ..goodwill import ITEM_URL, parse_item_id
from ..profit import evaluate_row
from ..telegram import local_time, money, time_left

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

log = logging.getLogger(__name__)

FLOAT_FIELDS = ["current_price", "minimum_bid", "buy_now_price", "goodwill_shipping", "ebay_price",
                "ebay_price_manual"]
INT_FIELDS = ["num_bids", "ebay_comps"]
TEXT_FIELDS = ["title", "url", "image_url", "category_name", "target_name", "ebay_query", "notes"]


def _float(v: Optional[str]) -> Optional[float]:
    v = (v or "").strip().replace("$", "").replace(",", "")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _int(v: Optional[str]) -> Optional[int]:
    f = _float(v)
    return int(f) if f is not None else None


def create_app(cfg: Dict[str, Any]) -> Flask:
    app = Flask(__name__)
    dcfg = cfg["dashboard"]
    app.secret_key = dcfg.get("secret_key") or secrets.token_hex(24)
    db = Database(cfg["general"]["db_path"])
    tz_name = cfg["general"]["timezone"]
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    state: Dict[str, Any] = {"engine": None}

    def engine():
        if state["engine"] is None:
            from ..worker import Engine
            state["engine"] = Engine(cfg, db)
        return state["engine"]

    # ------------------------------------------------------------------ security
    @app.before_request
    def _security():
        user, pwd = dcfg.get("username"), dcfg.get("password")
        if user or pwd:
            auth = request.authorization
            ok = auth and hmac.compare_digest(auth.username or "", str(user or "")) \
                and hmac.compare_digest(auth.password or "", str(pwd or ""))
            if not ok:
                return Response("Authentication required", 401,
                                {"WWW-Authenticate": 'Basic realm="GoodSkrapper"'})
        if request.method == "POST":
            token = request.form.get("_csrf", "")
            if not token or not hmac.compare_digest(token, session.get("_csrf", "")):
                abort(400, "Invalid CSRF token - reload the page and try again")

    def csrf_token() -> str:
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_hex(16)
        return session["_csrf"]

    def hours_left(value: Optional[str]) -> Optional[float]:
        dt = parse_iso(value)
        return (dt - datetime.now(timezone.utc)).total_seconds() / 3600 if dt else None

    @app.context_processor
    def _ctx():
        return {"csrf_token": csrf_token, "statuses": STATUSES, "money": money,
                "local_time": lambda v: local_time(v, tz_name), "time_left": time_left,
                "hours_left": hours_left, "urgent_h": float(cfg["telegram"]["urgent_threshold_hours"]),
                "target_pct": cfg["profit"]["target_profit_pct"]}

    @app.template_filter("pct")
    def _pct(v):
        return "—" if v is None else f"{float(v):.0f}%"

    def to_local_input(value: Optional[str]) -> str:
        dt = parse_iso(value)
        return dt.astimezone(tz).strftime("%Y-%m-%dT%H:%M") if dt else ""

    def from_local_input(value: str) -> Optional[str]:
        if not value:
            return None
        try:
            dt = datetime.strptime(value, "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
        except ValueError:
            return None
        return iso(dt)

    def form_to_row(form) -> Dict[str, Any]:
        row: Dict[str, Any] = {}
        for f in TEXT_FIELDS:
            if f in form:
                row[f] = form.get(f, "").strip()
        for f in FLOAT_FIELDS:
            if f in form:
                row[f] = _float(form.get(f))
        for f in INT_FIELDS:
            if f in form:
                row[f] = _int(form.get(f)) or 0
        if "end_time" in form:
            row["end_time"] = from_local_input(form.get("end_time", ""))
        if form.get("status") in STATUSES:
            row["status"] = form["status"]
        if form.get("listing_type") in ("auction", "buy_now"):
            row["listing_type"] = form["listing_type"]
        if "goodwill_shipping" in form:
            row["shipping_estimated"] = 0 if row.get("goodwill_shipping") is not None else 1
        return row

    def recompute(item_id: int) -> None:
        row = db.get_item(item_id)
        if row:
            db.update_item(item_id, evaluate_row(row, cfg, target_profit_for(cfg, row.get("target_name"))))

    # ------------------------------------------------------------------ list
    @app.route("/")
    def index():
        args = request.args
        page = max(1, _int(args.get("page")) or 1)
        size = int(dcfg.get("page_size", 50))
        active = args.get("active", "1") == "1"
        items, total = db.list_items(
            q=args.get("q", "").strip(), status=args.get("status", ""), active_only=active,
            deals_only=args.get("deals") == "1", sort=args.get("sort", "end_time"),
            desc=args.get("desc") == "1", limit=size, offset=(page - 1) * size)
        last = db.get_meta("last_scrape")
        try:
            last_stats = json.loads(db.get_meta("last_scrape_stats") or "{}")
        except ValueError:
            last_stats = {}
        return render_template(
            "index.html", items=items, total=total, page=page, pages=max(1, -(-total // size)),
            args=args, active=active, stats=db.stats(), last_scrape=last, last_stats=last_stats,
            run_requested=bool(db.get_meta("run_requested")))

    # ------------------------------------------------------------------ create
    @app.route("/item/new", methods=["GET", "POST"])
    def item_new():
        if request.method == "GET":
            return render_template("form.html", item={}, is_new=True, end_local="")
        form = request.form
        item_id = parse_item_id(form.get("item_ref", ""))
        if not item_id:
            flash("Enter a valid ShopGoodwill item ID or URL.", "error")
            return render_template("form.html", item=form_to_row(form), is_new=True,
                                   end_local=form.get("end_time", "")), 400
        if db.get_item(item_id):
            flash(f"Item {item_id} already exists - edit it instead.", "error")
            return redirect(url_for("item_edit", item_id=item_id))
        overrides = {k: v for k, v in form_to_row(form).items() if v not in (None, "")}
        if overrides.get("goodwill_shipping") is None:
            overrides.pop("shipping_estimated", None)
        for f in INT_FIELDS:
            if not overrides.get(f):
                overrides.pop(f, None)
        fetched = None
        if form.get("fetch") == "1":
            try:
                fetched = engine().refresh_item(item_id)
            except Exception as exc:
                log.exception("Fetch failed")
                flash(f"Could not fetch from Goodwill: {exc}", "error")
        if fetched:
            db.update_item(item_id, overrides)
            flash(f"Item {item_id} added from ShopGoodwill.", "ok")
        else:
            if not overrides.get("title"):
                flash("Title is required when the item is not fetched from Goodwill.", "error")
                return render_template("form.html", item=form_to_row(form), is_new=True,
                                       end_local=form.get("end_time", "")), 400
            row = {"item_id": item_id, "url": ITEM_URL.format(item_id), "source": "manual",
                   "status": "watching", **overrides}
            db.insert_item(row)
            flash(f"Item {item_id} added manually.", "ok")
        recompute(item_id)
        return redirect(url_for("item_edit", item_id=item_id))

    # ------------------------------------------------------------------ read/update
    @app.route("/item/<int:item_id>", methods=["GET", "POST"])
    def item_edit(item_id: int):
        item = db.get_item(item_id)
        if not item:
            abort(404)
        if request.method == "POST":
            row = form_to_row(request.form)
            if request.form.get("reset_notifications") == "1":
                row["notified_at"] = None
                row["urgent_notified_at"] = None
            db.update_item(item_id, row)
            recompute(item_id)
            flash("Item saved.", "ok")
            return redirect(url_for("item_edit", item_id=item_id))
        return render_template("form.html", item=item, is_new=False, end_local=to_local_input(item.get("end_time")))

    @app.route("/item/<int:item_id>/status", methods=["POST"])
    def item_status(item_id: int):
        status = request.form.get("status", "")
        if status in STATUSES:
            db.set_status([item_id], status)
        return redirect(request.form.get("next") or url_for("index"))

    @app.route("/item/<int:item_id>/refresh", methods=["POST"])
    def item_refresh(item_id: int):
        try:
            engine().refresh_item(item_id, reprice=request.form.get("reprice") == "1")
            flash("Item refreshed from ShopGoodwill" + (" and eBay." if request.form.get("reprice") else "."), "ok")
        except Exception as exc:
            log.exception("Refresh failed")
            flash(f"Refresh failed: {exc}", "error")
        return redirect(request.form.get("next") or url_for("item_edit", item_id=item_id))

    # ------------------------------------------------------------------ delete
    @app.route("/item/<int:item_id>/delete", methods=["POST"])
    def item_delete(item_id: int):
        db.delete_items([item_id])
        flash(f"Item {item_id} deleted.", "ok")
        return redirect(url_for("index"))

    @app.route("/bulk", methods=["POST"])
    def bulk():
        ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
        action = request.form.get("action", "")
        if not ids:
            flash("No items selected.", "error")
        elif action == "delete":
            flash(f"Deleted {db.delete_items(ids)} items.", "ok")
        elif action.startswith("status:") and action[7:] in STATUSES:
            db.set_status(ids, action[7:])
            flash(f"Updated {len(ids)} items.", "ok")
        elif action == "renotify":
            for i in ids:
                db.update_item(i, {"notified_at": None, "urgent_notified_at": None})
            flash(f"{len(ids)} items will be included in the next Telegram update.", "ok")
        return redirect(request.form.get("next") or url_for("index"))

    # ------------------------------------------------------------------ misc
    @app.route("/run-now", methods=["POST"])
    def run_now():
        db.set_meta("run_requested", iso(datetime.now(timezone.utc)))
        flash("Scrape requested - the worker will start it within ~15 seconds.", "ok")
        return redirect(url_for("index"))

    @app.route("/settings")
    def settings():
        import yaml
        text = yaml.safe_dump({k: v for k, v in masked(cfg).items() if not k.startswith("_")},
                              sort_keys=False, allow_unicode=True)
        return render_template("settings.html", config_text=text, config_path=cfg.get("_path"))

    @app.route("/api/items")
    def api_items():
        items, total = db.list_items(q=request.args.get("q", ""), status=request.args.get("status", ""),
                                     active_only=request.args.get("active") == "1",
                                     deals_only=request.args.get("deals") == "1", limit=500)
        return jsonify({"total": total, "items": items})

    @app.route("/healthz")
    def healthz():
        return {"ok": True, "last_scrape": db.get_meta("last_scrape")}

    return app


def serve(cfg: Dict[str, Any]) -> None:
    app = create_app(cfg)
    host, port = cfg["dashboard"]["host"], int(cfg["dashboard"]["port"])
    try:
        from waitress import serve as wserve
        log.info("Dashboard on http://%s:%s (waitress)", host, port)
        wserve(app, host=host, port=port, threads=4)
    except ImportError:
        log.warning("waitress not installed, using Flask dev server")
        app.run(host=host, port=port)

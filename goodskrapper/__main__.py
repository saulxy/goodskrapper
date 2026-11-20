"""Command line entry point:  python -m goodskrapper <command>"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from typing import Any, Dict

from .config import ConfigError, load_config


def setup_logging(cfg: Dict[str, Any], verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else getattr(logging, str(cfg["general"]["log_level"]).upper(), logging.INFO)
    handlers: list = [logging.StreamHandler(sys.stdout)]
    if cfg["general"].get("log_file"):
        handlers.append(RotatingFileHandler(cfg["general"]["log_file"], maxBytes=1_000_000, backupCount=3,
                                            encoding="utf-8"))
    logging.basicConfig(level=level, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for noisy in ("urllib3", "waitress"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _install_signals(stop) -> None:
    def handler(signum, frame):
        logging.getLogger("goodskrapper").info("Signal %s received, stopping", signum)
        stop()
    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handler)


def cmd_worker(cfg, args) -> int:
    from .worker import Worker
    w = Worker(cfg)
    _install_signals(w.stop)
    w.run_forever()
    return 0


def cmd_dashboard(cfg, args) -> int:
    from .dashboard import serve
    serve(cfg)
    return 0


def cmd_all(cfg, args) -> int:
    from .dashboard import serve
    from .worker import Worker
    w = Worker(cfg)
    t = threading.Thread(target=w.run_forever, name="worker", daemon=True)
    t.start()
    serve(cfg)  # blocks; Ctrl+C stops everything
    w.stop()
    return 0


def cmd_scrape_once(cfg, args) -> int:
    from .worker import Engine
    e = Engine(cfg)
    print(json.dumps(e.scrape_cycle(), indent=2))
    if args.notify:
        e.send_urgent()
        e.send_digest()
    return 0


def cmd_digest(cfg, args) -> int:
    from .worker import Engine
    e = Engine(cfg)
    print(f"urgent sent: {e.send_urgent()}  digest items: {e.send_digest()}")
    return 0


def cmd_telegram_test(cfg, args) -> int:
    from .telegram import TelegramClient
    tg = TelegramClient(cfg["telegram"])
    if not tg.token:
        print("telegram.bot_token is empty"); return 1
    me = tg.get_me()
    print(f"Bot OK: @{me.get('username')} ({me.get('first_name')})")
    if not tg.chat_id:
        print("telegram.chat_id is empty - run `telegram-chat-id`"); return 1
    tg.send_message("✅ <b>GoodSkrapper</b> is connected to this channel.")
    print(f"Test message sent to {tg.chat_id}")
    return 0


def cmd_telegram_chat_id(cfg, args) -> int:
    from .telegram import TelegramClient
    tg = TelegramClient(cfg["telegram"])
    if not tg.token:
        print("telegram.bot_token is empty"); return 1
    found = {}
    for upd in tg.get_updates():
        for key in ("channel_post", "message", "my_chat_member"):
            chat = (upd.get(key) or {}).get("chat")
            if chat:
                found[chat["id"]] = f"{chat.get('type')}: {chat.get('title') or chat.get('username') or chat.get('first_name')}"
    if not found:
        print("No chats found. Add the bot as admin of your channel, post a message there, and retry.")
        return 1
    for cid, desc in found.items():
        print(f"{cid}\t{desc}")
    return 0


def cmd_categories(cfg, args) -> int:
    from .goodwill import GoodwillClient
    gw = GoodwillClient(cfg["scraper"])
    for c in gw.categories():
        if args.filter and args.filter.lower() not in c["name"].lower():
            continue
        print(f"{c['id']:>6}  {'  ' * (int(c['level'] or 1) - 1)}{c['name']}")
    return 0


def cmd_price(cfg, args) -> int:
    from .db import Database
    from .ebay import EbayPricer, build_query
    pricer = EbayPricer(cfg["ebay"], Database(cfg["general"]["db_path"]))
    q = build_query(args.title, int(cfg["ebay"]["query_max_words"]))
    print(f"Query: {q}")
    print(json.dumps(pricer.estimate(args.title, use_cache=False), indent=2))
    return 0


def cmd_cleanup(cfg, args) -> int:
    from .worker import Engine
    print(Engine(cfg).cleanup())
    return 0


COMMANDS = {
    "worker": (cmd_worker, "Run the scraper + Telegram scheduler (use with systemd)"),
    "dashboard": (cmd_dashboard, "Run the web dashboard"),
    "all": (cmd_all, "Run worker and dashboard in one process"),
    "scrape-once": (cmd_scrape_once, "Run a single scrape cycle and exit"),
    "digest": (cmd_digest, "Send pending Telegram alerts now"),
    "telegram-test": (cmd_telegram_test, "Verify bot token and send a test message"),
    "telegram-chat-id": (cmd_telegram_chat_id, "Discover your channel chat_id"),
    "categories": (cmd_categories, "List ShopGoodwill category IDs"),
    "price": (cmd_price, "Test eBay price lookup for a title"),
    "cleanup": (cmd_cleanup, "Apply DB retention now"),
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="goodskrapper", description="ShopGoodwill → eBay flip finder")
    parser.add_argument("-c", "--config", help="Path to config.yaml (default: ./config.yaml or $GOODSKRAPPER_CONFIG)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, (_, help_text) in COMMANDS.items():
        p = sub.add_parser(name, help=help_text)
        if name == "scrape-once":
            p.add_argument("--notify", action="store_true", help="Also send Telegram alerts")
        if name == "categories":
            p.add_argument("filter", nargs="?", default="", help="Only show names containing this text")
        if name == "price":
            p.add_argument("title", help="Item title to price on eBay")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg, args.verbose)
    return COMMANDS[args.command][0](cfg, args) or 0


if __name__ == "__main__":
    sys.exit(main())

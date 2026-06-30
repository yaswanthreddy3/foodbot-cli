#!/usr/bin/env python3
"""
╔══════════════════════════════════════════╗
║   🍕 FoodBot CLI — Swiggy + Zomato      ║
║   mcp-remote · Ollama · gpt-oss:20b     ║
╚══════════════════════════════════════════╝
"""

import asyncio, json, os, subprocess, logging, re
from pathlib import Path
import warnings, threading, concurrent.futures
warnings.filterwarnings("ignore", category=DeprecationWarning)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.rule import Rule
from rich.prompt import Prompt
from rich import box
from rich.markup import escape
from prompt_toolkit import PromptSession
from prompt_toolkit.styles import Style as PTStyle
from prompt_toolkit.formatted_text import HTML

import ollama as ollama_sdk          # pip install ollama
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

console = Console()

# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
TOKEN_FILE   = SCRIPT_DIR / "tokens.json"
MCP_AUTH_DIR = Path.home() / ".mcp-auth"

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.environ.get("OLLAMA_MODEL", "gpt-oss:20b")

# How many tool-call rounds before we force a final answer
MAX_ROUNDS = 8

MCP_SERVERS = {
    "zomato":           "https://mcp-server.zomato.com/mcp",
    "swiggy_food":      "https://mcp.swiggy.com/food",
    "swiggy_instamart": "https://mcp.swiggy.com/im",
    "swiggy_dineout":   "https://mcp.swiggy.com/dineout",
}
SWIGGY_KEYS = ("swiggy_food", "swiggy_instamart", "swiggy_dineout")
TOKENS: dict[str, str] = {k: "" for k in MCP_SERVERS}
MCP_LOGIN_URLS = {
    "zomato": "https://mcp-server.zomato.com/mcp",
    "swiggy": "https://mcp.swiggy.com/food",
}


# Confirmation state
ALWAYS_ALLOW: set[str] = set()
ALLOW_ALL = False
_confirm_lock = threading.Lock()

# Async loop for MCP
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()

def _run_async(coro) -> str:
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return fut.result(timeout=30)
    except concurrent.futures.TimeoutError:
        return "[Timeout — MCP call took too long]"
    except Exception as e:
        return f"[MCP Error: {e}]"

# ─────────────────────────────────────────
#  TOKEN HELPERS
# ─────────────────────────────────────────
def _extract_token(data) -> str:
    if not isinstance(data, dict): return ""
    for k in ("access_token","token","accessToken","id_token","bearer"):
        if isinstance(data.get(k), str) and data[k]: return data[k]
    for k in ("tokens","credentials","data","result"):
        found = _extract_token(data.get(k))
        if found: return found
    return ""

def _identify_platform(file_path: Path) -> str | None:
    scope = str(file_path).lower()
    prefix = file_path.name.split("_tokens.json")[0]
    for sib in file_path.parent.glob(f"{prefix}*"):
        try: scope += sib.read_text().lower()
        except: pass
    if "zomato" in scope: return "zomato"
    if "swiggy" in scope: return "swiggy"
    return None

def load_tokens(verbose=False) -> bool:
    loaded = []
    if MCP_AUTH_DIR.exists():
        for tf in sorted(MCP_AUTH_DIR.rglob("*_tokens.json")):
            try:
                token = _extract_token(json.loads(tf.read_text()))
                if not token: continue
                platform = _identify_platform(tf)
                if platform == "zomato" and not TOKENS["zomato"]:
                    TOKENS["zomato"] = token; loaded.append("zomato")
                    if verbose: console.print("  [green]✅  zomato loaded from mcp-remote[/]")
                elif platform == "swiggy":
                    for k in SWIGGY_KEYS:
                        if not TOKENS[k]: TOKENS[k] = token; loaded.append(k)
                    if verbose: console.print("  [green]✅  swiggy loaded from mcp-remote[/]")
            except Exception as e:
                if verbose: console.print(f"  [red]⚠️  {tf.name}: {e}[/]")
    if TOKEN_FILE.exists():
        try:
            d = json.loads(TOKEN_FILE.read_text())
            if d.get("zomato") and not TOKENS["zomato"]:
                TOKENS["zomato"] = d["zomato"]; loaded.append("zomato")
                if verbose: console.print("  [green]✅ [zomato] loaded from tokens.json[/]")
            if d.get("swiggy"):
                for k in SWIGGY_KEYS:
                    if not TOKENS[k]: TOKENS[k] = d["swiggy"]; loaded.append(k)
                if verbose: console.print("  [green]✅ [swiggy] loaded from tokens.json[/]")
        except Exception as e:
            if verbose: console.print(f"  [red]⚠️  tokens.json: {e}[/]")
    return bool(loaded)

def save_tokens():
    try:
        TOKEN_FILE.write_text(json.dumps({"zomato": TOKENS["zomato"], "swiggy": TOKENS["swiggy_food"]}, indent=2))
        TOKEN_FILE.chmod(0o600)
        console.print(f"  [dim]💾 Saved → {TOKEN_FILE}[/]")
    except Exception as e:
        console.print(f"  [red]⚠️  Save failed: {e}[/]")

def clear_tokens():
    for k in TOKENS: TOKENS[k] = ""
    if TOKEN_FILE.exists(): TOKEN_FILE.unlink()
    console.print("  [yellow]🗑️  All tokens cleared.[/]")

def _set_swiggy(token: str):
    for k in SWIGGY_KEYS: TOKENS[k] = token

# ─────────────────────────────────────────
#  MCP CALLER
# ─────────────────────────────────────────
async def _mcp_call(server_key: str, tool_name: str, args: dict) -> str:
    url   = MCP_SERVERS[server_key]
    token = TOKENS.get(server_key, "")
    hdrs  = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with streamablehttp_client(url, headers=hdrs) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool(tool_name, args)
                return "\n".join(c.text for c in res.content if hasattr(c,"text")) or "(empty)"
    except Exception as e:
        return f"[MCP Error — {server_key}/{tool_name}: {e}]"

async def _mcp_list_tools(server_key: str):
    url   = MCP_SERVERS[server_key]
    token = TOKENS.get(server_key, "")
    hdrs  = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with streamablehttp_client(url, headers=hdrs) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.list_tools()
                return [(t.name, t.description) for t in res.tools]
    except Exception as e:
        return [("error", str(e))]

MAX_RESULT_CHARS = 3000

def _do_mcp(key: str, name: str, args: dict) -> str:
    if not TOKENS.get(key):
        plat = "zomato" if "zomato" in key else "swiggy"
        return f"[Not logged in to {plat} — run: login {plat}]"
    console.print(f"  [dim cyan]⚡ calling {name}...[/]")
    result = _run_async(_mcp_call(key, name, args))
    if len(result) > MAX_RESULT_CHARS:
        result = result[:MAX_RESULT_CHARS] + "\n...[truncated]"
    return result

# ─────────────────────────────────────────
#  TOOL CONFIRMATION
# ─────────────────────────────────────────
def tool_confirm(tool_name: str, server: str, args: dict) -> bool:
    global ALLOW_ALL
    console.print()
    arg_text = "\n".join(f"  [dim]{k}:[/] {escape(str(v))}" for k,v in args.items()) or "  [dim](no args)[/]"
    console.print(Panel(
        f"[bold yellow]Server:[/] {server}  [bold yellow]Tool:[/] {tool_name}\n\n[bold yellow]Args:[/]\n{arg_text}",
        title="[bold]⚡ Tool Call[/]", border_style="yellow", padding=(0,2)
    ))
    for num, label in [("1","Allow once"),("2","Always allow this tool"),("3","Allow all tools"),("4","Skip")]:
        console.print(f"  {'[bold green]' if num=='1' else '[dim]'}{num}. {label}[/]")
    choice = Prompt.ask("\n  [bold]Allow?[/]", choices=["1","2","3","4"], default="1")
    console.print()
    if choice == "2": ALWAYS_ALLOW.add(tool_name)
    elif choice == "3": ALLOW_ALL = True
    return choice != "4"

def _confirm_and_call(tool_name: str, server_key: str, mcp_name: str, args: dict) -> str:
    if not ALLOW_ALL and tool_name not in ALWAYS_ALLOW:
        with _confirm_lock:
            if not ALLOW_ALL and tool_name not in ALWAYS_ALLOW:
                if not tool_confirm(tool_name, server_key.replace("_","-"), args):
                    return "[Skipped by user]"
    return _do_mcp(server_key, mcp_name, args)

# ─────────────────────────────────────────
#  TOOL HANDLERS
# ─────────────────────────────────────────
def _h_zomato_addresses(a):
    return _confirm_and_call("zomato_addresses", "zomato", "get_saved_addresses_for_user", {})

def _h_zomato_search(a):
    p = {"query": a["query"]}
    if a.get("address_id"):    p["address_id"]  = a["address_id"]
    if a.get("min_rating"):    p["min_rating"]  = a["min_rating"]
    if a.get("max_price"):     p["max_price"]   = a["max_price"]
    if a.get("near_and_fast"): p["near_and_fast"] = a["near_and_fast"]
    if a.get("offers_tag"):    p["offers_tag"]  = a["offers_tag"]
    return _confirm_and_call("zomato_search", "zomato", "get_restaurants_for_keyword", p)

def _h_zomato_menu_listing(a):
    return _confirm_and_call("zomato_menu_listing", "zomato", "get_menu_items_listing", {"restaurant_id": a["restaurant_id"]})

def _h_zomato_menu_by_category(a):
    p = {"restaurant_id": a["restaurant_id"]}
    if a.get("categories"): p["categories"] = a["categories"]
    return _confirm_and_call("zomato_menu_by_category", "zomato", "get_restaurant_menu_by_categories", p)

def _h_zomato_create_cart(a):
    return _confirm_and_call("zomato_create_cart", "zomato", "create_cart",
        {"restaurant_id": a["restaurant_id"], "items": a["items"],
         "address_id": a["address_id"], "payment_method": a["payment_method"]})

def _h_zomato_cart_offers(a):
    return _confirm_and_call("zomato_cart_offers", "zomato", "get_cart_offers", {"cart_id": a["cart_id"]})

def _h_zomato_checkout(a):
    return _confirm_and_call("zomato_checkout", "zomato", "checkout_cart", {"cart_id": a["cart_id"]})

def _h_zomato_track_order(a):
    return _confirm_and_call("zomato_track_order", "zomato", "get_order_tracking_info", {})

def _h_zomato_order_history(a):
    return _confirm_and_call("zomato_order_history", "zomato", "get_order_history", {})

def _h_swiggy_addresses(a):
    return _confirm_and_call("swiggy_addresses", "swiggy_food", "get_addresses", {})

def _h_swiggy_search(a):
    p = {"query": a["query"]}
    if a.get("address_id"): p["addressId"] = a["address_id"]
    return _confirm_and_call("swiggy_search", "swiggy_food", "search_restaurants", p)

def _h_swiggy_search_menu(a):
    p = {"query": a["query"]}
    if a.get("restaurant_id"): p["restaurantId"] = a["restaurant_id"]
    if a.get("address_id"):    p["addressId"]    = a["address_id"]
    return _confirm_and_call("swiggy_search_menu", "swiggy_food", "search_menu", p)

def _h_swiggy_restaurant_menu(a):
    return _confirm_and_call("swiggy_restaurant_menu", "swiggy_food", "get_restaurant_menu",
        {"restaurantId": a["restaurant_id"], "addressId": a.get("address_id",""), "page": a.get("page",1)})

def _h_swiggy_view_cart(a):
    return _confirm_and_call("swiggy_view_cart", "swiggy_food", "get_food_cart", {})

def _h_swiggy_update_cart(a):
    return _confirm_and_call("swiggy_update_cart", "swiggy_food", "update_food_cart",
        {"restaurantId": a["restaurant_id"], "items": a["items"], "addressId": a.get("address_id","")})

def _h_swiggy_clear_cart(a):
    return _confirm_and_call("swiggy_clear_cart", "swiggy_food", "flush_food_cart", {})

def _h_swiggy_fetch_coupons(a):
    return _confirm_and_call("swiggy_fetch_coupons", "swiggy_food", "fetch_food_coupons",
        {"restaurantId": a["restaurant_id"], "addressId": a["address_id"]})

def _h_swiggy_apply_coupon(a):
    return _confirm_and_call("swiggy_apply_coupon", "swiggy_food", "apply_food_coupon",
        {"couponCode": a["coupon_code"], "addressId": a["address_id"]})

def _h_swiggy_place_order(a):
    return _confirm_and_call("swiggy_place_order", "swiggy_food", "place_food_order", {"addressId": a["address_id"]})

def _h_swiggy_active_orders(a):
    return _confirm_and_call("swiggy_active_orders", "swiggy_food", "get_food_orders", {"addressId": a.get("address_id","")})

def _h_swiggy_track_order(a):
    p = {}
    if a.get("order_id"): p["orderId"] = a["order_id"]
    return _confirm_and_call("swiggy_track_order", "swiggy_food", "track_food_order", p)

def _h_swiggy_order_details(a):
    return _confirm_and_call("swiggy_order_details", "swiggy_food", "get_food_order_details", {"orderId": a["order_id"]})

def _h_instamart_search(a):
    return _confirm_and_call("instamart_search", "swiggy_instamart", "search_products", {"query": a["query"]})

def _h_instamart_categories(a):
    return _confirm_and_call("instamart_categories", "swiggy_instamart", "get_categories", {})

def _h_instamart_add_to_cart(a):
    return _confirm_and_call("instamart_add_to_cart", "swiggy_instamart", "add_to_cart",
        {"product_id": a["product_id"], "quantity": a["quantity"]})

def _h_instamart_view_cart(a):
    return _confirm_and_call("instamart_view_cart", "swiggy_instamart", "get_cart", {})

def _h_instamart_place_order(a):
    return _confirm_and_call("instamart_place_order", "swiggy_instamart", "place_order",
        {"address_id": a["address_id"], "payment_method": a["payment_method"]})

def _h_dineout_search(a):
    return _confirm_and_call("dineout_search", "swiggy_dineout", "search_restaurants",
        {"query": a["query"], "city": a.get("city",""), "cuisine": a.get("cuisine","")})

def _h_dineout_offers(a):
    return _confirm_and_call("dineout_offers", "swiggy_dineout", "get_offers", {"restaurant_id": a["restaurant_id"]})

def _h_dineout_book_table(a):
    return _confirm_and_call("dineout_book_table", "swiggy_dineout", "book_table",
        {"restaurant_id": a["restaurant_id"], "date": a["date"], "time": a["time"], "guests": a["guests"]})

def _h_compare_prices(a):
    dish         = a["dish_name"]
    swiggy_addr  = a.get("swiggy_address_id", "")
    zomato_addr  = a.get("zomato_address_id", "")

    console.print(f"  [dim cyan]⚡ compare_prices: searching both platforms for '{dish}'...[/]")
    if not ALLOW_ALL and "compare_prices" not in ALWAYS_ALLOW:
        with _confirm_lock:
            if not ALLOW_ALL and "compare_prices" not in ALWAYS_ALLOW:
                if not tool_confirm("compare_prices", "swiggy+zomato", a):
                    return "[Skipped by user]"

    sw_args = {"query": dish}
    if swiggy_addr:
        sw_args["addressId"] = swiggy_addr

    zo_args = {"query": dish}
    if zomato_addr:
        zo_args["address_id"] = zomato_addr

    sw = _do_mcp("swiggy_food", "search_restaurants", sw_args)
    zo = _do_mcp("zomato", "get_restaurants_for_keyword", zo_args)
    return f"━━ SWIGGY ━━\n{sw}\n\n━━ ZOMATO ━━\n{zo}"

# ── Handler registry ──────────────────────────────────────────
TOOL_HANDLERS: dict[str, callable] = {
    "zomato_addresses":        _h_zomato_addresses,
    "zomato_search":           _h_zomato_search,
    "zomato_menu_listing":     _h_zomato_menu_listing,
    "zomato_menu_by_category": _h_zomato_menu_by_category,
    "zomato_create_cart":      _h_zomato_create_cart,
    "zomato_cart_offers":      _h_zomato_cart_offers,
    "zomato_checkout":         _h_zomato_checkout,
    "zomato_track_order":      _h_zomato_track_order,
    "zomato_order_history":    _h_zomato_order_history,
    "swiggy_addresses":        _h_swiggy_addresses,
    "swiggy_search":           _h_swiggy_search,
    "swiggy_search_menu":      _h_swiggy_search_menu,
    "swiggy_restaurant_menu":  _h_swiggy_restaurant_menu,
    "swiggy_view_cart":        _h_swiggy_view_cart,
    "swiggy_update_cart":      _h_swiggy_update_cart,
    "swiggy_clear_cart":       _h_swiggy_clear_cart,
    "swiggy_fetch_coupons":    _h_swiggy_fetch_coupons,
    "swiggy_apply_coupon":     _h_swiggy_apply_coupon,
    "swiggy_place_order":      _h_swiggy_place_order,
    "swiggy_active_orders":    _h_swiggy_active_orders,
    "swiggy_track_order":      _h_swiggy_track_order,
    "swiggy_order_details":    _h_swiggy_order_details,
    "instamart_search":        _h_instamart_search,
    "instamart_categories":    _h_instamart_categories,
    "instamart_add_to_cart":   _h_instamart_add_to_cart,
    "instamart_view_cart":     _h_instamart_view_cart,
    "instamart_place_order":   _h_instamart_place_order,
    "dineout_search":          _h_dineout_search,
    "dineout_offers":          _h_dineout_offers,
    "dineout_book_table":      _h_dineout_book_table,
    "compare_prices":          _h_compare_prices,
}

# ── Ollama tool schemas ───────────────────────────────────────
OLLAMA_TOOLS = [
    {"type":"function","function":{"name":"zomato_addresses","description":"Get saved Zomato delivery addresses. Call FIRST before any Zomato search.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"zomato_search","description":"Search restaurants on Zomato by keyword.","parameters":{"type":"object","required":["query"],"properties":{"query":{"type":"string"},"address_id":{"type":"string","description":"Zomato address ID from zomato_addresses"},"min_rating":{"type":"number"},"max_price":{"type":"integer"},"near_and_fast":{"type":"boolean"},"offers_tag":{"type":"string"}}}}},
    {"type":"function","function":{"name":"zomato_menu_listing","description":"Get menu categories and dish names for a Zomato restaurant.","parameters":{"type":"object","required":["restaurant_id"],"properties":{"restaurant_id":{"type":"string"}}}}},
    {"type":"function","function":{"name":"zomato_menu_by_category","description":"Get full Zomato menu with prices filtered by categories.","parameters":{"type":"object","required":["restaurant_id"],"properties":{"restaurant_id":{"type":"string"},"categories":{"type":"array","items":{"type":"string"}}}}}},
    {"type":"function","function":{"name":"zomato_create_cart","description":"Create a Zomato cart. Always confirm with user first.","parameters":{"type":"object","required":["restaurant_id","items","address_id","payment_method"],"properties":{"restaurant_id":{"type":"string"},"items":{"type":"array"},"address_id":{"type":"string"},"payment_method":{"type":"string"}}}}},
    {"type":"function","function":{"name":"zomato_cart_offers","description":"Get coupons for a Zomato cart.","parameters":{"type":"object","required":["cart_id"],"properties":{"cart_id":{"type":"string"}}}}},
    {"type":"function","function":{"name":"zomato_checkout","description":"Place a Zomato order. Confirm with user first.","parameters":{"type":"object","required":["cart_id"],"properties":{"cart_id":{"type":"string"}}}}},
    {"type":"function","function":{"name":"zomato_track_order","description":"Track active Zomato orders.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"zomato_order_history","description":"Get Zomato order history.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"swiggy_addresses","description":"Get saved Swiggy delivery addresses. Call FIRST before any Swiggy search.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"swiggy_search","description":"Search restaurants on Swiggy.","parameters":{"type":"object","required":["query"],"properties":{"query":{"type":"string"},"address_id":{"type":"string","description":"Swiggy address ID from swiggy_addresses"}}}}},
    {"type":"function","function":{"name":"swiggy_search_menu","description":"Search for specific dishes on Swiggy.","parameters":{"type":"object","required":["query"],"properties":{"query":{"type":"string"},"restaurant_id":{"type":"string"},"address_id":{"type":"string"}}}}},
    {"type":"function","function":{"name":"swiggy_restaurant_menu","description":"Browse full menu of a Swiggy restaurant.","parameters":{"type":"object","required":["restaurant_id"],"properties":{"restaurant_id":{"type":"string"},"address_id":{"type":"string"},"page":{"type":"integer"}}}}},
    {"type":"function","function":{"name":"swiggy_view_cart","description":"View current Swiggy cart.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"swiggy_update_cart","description":"Add/update items in Swiggy cart.","parameters":{"type":"object","required":["restaurant_id","items"],"properties":{"restaurant_id":{"type":"string"},"items":{"type":"array"},"address_id":{"type":"string"}}}}},
    {"type":"function","function":{"name":"swiggy_clear_cart","description":"Clear Swiggy cart.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"swiggy_fetch_coupons","description":"Get coupons for Swiggy order (COD only).","parameters":{"type":"object","required":["restaurant_id","address_id"],"properties":{"restaurant_id":{"type":"string"},"address_id":{"type":"string"}}}}},
    {"type":"function","function":{"name":"swiggy_apply_coupon","description":"Apply coupon to Swiggy order.","parameters":{"type":"object","required":["coupon_code","address_id"],"properties":{"coupon_code":{"type":"string"},"address_id":{"type":"string"}}}}},
    {"type":"function","function":{"name":"swiggy_place_order","description":"Place Swiggy order (COD only, max Rs.999). ALWAYS confirm with user first.","parameters":{"type":"object","required":["address_id"],"properties":{"address_id":{"type":"string"}}}}},
    {"type":"function","function":{"name":"swiggy_active_orders","description":"Get active Swiggy orders.","parameters":{"type":"object","properties":{"address_id":{"type":"string"}}}}},
    {"type":"function","function":{"name":"swiggy_track_order","description":"Track a Swiggy order.","parameters":{"type":"object","properties":{"order_id":{"type":"string"}}}}},
    {"type":"function","function":{"name":"swiggy_order_details","description":"Get details of a specific Swiggy order.","parameters":{"type":"object","required":["order_id"],"properties":{"order_id":{"type":"string"}}}}},
    {"type":"function","function":{"name":"instamart_search","description":"Search groceries on Swiggy Instamart.","parameters":{"type":"object","required":["query"],"properties":{"query":{"type":"string"}}}}},
    {"type":"function","function":{"name":"instamart_categories","description":"Browse Instamart product categories.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"instamart_add_to_cart","description":"Add item to Instamart cart.","parameters":{"type":"object","required":["product_id","quantity"],"properties":{"product_id":{"type":"string"},"quantity":{"type":"integer"}}}}},
    {"type":"function","function":{"name":"instamart_view_cart","description":"View Instamart cart.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"instamart_place_order","description":"Place Instamart grocery order.","parameters":{"type":"object","required":["address_id","payment_method"],"properties":{"address_id":{"type":"string"},"payment_method":{"type":"string"}}}}},
    {"type":"function","function":{"name":"dineout_search","description":"Search dine-out restaurants on Swiggy Dineout.","parameters":{"type":"object","required":["query"],"properties":{"query":{"type":"string"},"city":{"type":"string"},"cuisine":{"type":"string"}}}}},
    {"type":"function","function":{"name":"dineout_offers","description":"Get dine-out deals at a restaurant.","parameters":{"type":"object","required":["restaurant_id"],"properties":{"restaurant_id":{"type":"string"}}}}},
    {"type":"function","function":{"name":"dineout_book_table","description":"Book a table via Swiggy Dineout.","parameters":{"type":"object","required":["restaurant_id","date","time","guests"],"properties":{"restaurant_id":{"type":"string"},"date":{"type":"string"},"time":{"type":"string"},"guests":{"type":"integer"}}}}},
    # ── compare_prices now accepts address IDs for both platforms ──
    {"type":"function","function":{"name":"compare_prices","description":"Search for a dish on BOTH Swiggy AND Zomato simultaneously in ONE call. Always use this for cross-platform comparisons. Pass address IDs obtained from swiggy_addresses and zomato_addresses.","parameters":{"type":"object","required":["dish_name"],"properties":{
        "dish_name":         {"type":"string"},
        "swiggy_address_id": {"type":"string","description":"Swiggy address ID from swiggy_addresses (e.g. d3npm3033da30lmsrtv0)"},
        "zomato_address_id": {"type":"string","description":"Zomato address ID from zomato_addresses (e.g. 855292940)"}
    }}}},
]

# ─────────────────────────────────────────
#  SYSTEM PROMPT
# ─────────────────────────────────────────
SYSTEM_PROMPT = """You are FoodBot 🍕, a food ordering assistant with access to live Swiggy and Zomato data via tools.

## WHAT TO DO WHEN USER ASKS ABOUT FOOD
When the user asks anything food-related, you MUST call a tool. Do not respond with text first.

Step-by-step for "find biryani and compare":
1. Call zomato_addresses() → note the address_id
2. Call swiggy_addresses() → note the address_id
3. Call compare_prices(dish_name="hyderabadi biryani", swiggy_address_id="<id>", zomato_address_id="<id>")
4. Read the tool results and write your answer. STOP calling tools after step 3.

Step-by-step for "search on one platform":
1. Call {platform}_addresses() → note the address_id
2. Call {platform}_search(query="dish name", address_id="<id>")
3. Write your answer. STOP.

## STRICT RULES
- Call each tool ONCE. Never call the same tool twice with same args.
- Always pass address_id when searching — it is REQUIRED by both platforms.
- compare_prices searches BOTH Swiggy and Zomato in ONE call — use it for comparisons.
- Only show data that tools actually returned. Never invent prices, ratings, or coupons.
- Never say "I will now search" — just call the tool immediately.
- For orders: confirm with user before calling place_order.
- For coupons: only show what zomato_cart_offers or swiggy_fetch_coupons returns.

## OUTPUT FORMAT FOR COMPARISONS
After getting tool results, present:
- Restaurant name, rating, dish price
- Swiggy: base price + delivery + taxes = final price
- Zomato: base price + delivery + taxes = final price
- Which platform is cheaper and by how much
"""

# ─────────────────────────────────────────
#  AGENT LOOP  (native Ollama SDK)
# ─────────────────────────────────────────
_current_model: str = OLLAMA_MODEL

def _check_ollama_running() -> bool:
    try:
        ollama_sdk.Client(host=OLLAMA_BASE_URL).list()
        return True
    except Exception:
        return False

def _list_ollama_models() -> list[str]:
    try:
        resp = ollama_sdk.Client(host=OLLAMA_BASE_URL).list()
        return [m.model for m in resp.models]
    except Exception:
        return []

def _strip_think(text: str) -> str:
    """Remove <think>...</think> reasoning tokens that gpt-oss:20b emits."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

def _collect_tool_results(messages: list[dict]) -> str:
    """Extract all tool results from message history into a single readable string."""
    parts = []
    for m in messages:
        if m.get("role") == "tool":
            name = m.get("name", "tool")
            content = m.get("content", "")
            parts.append("[" + name + " result]\n" + content)
    return "\n\n".join(parts)

def _summarize_with_fresh_call(client, user_question: str, tool_results: str) -> str:
    """
    Ask the model to summarize collected tool data using a FRESH message thread.
    This bypasses gpt-oss:20b going silent after large tool payloads.
    """
    if len(tool_results) > 4000:
        tool_results = tool_results[:4000] + "\n...[truncated]"

    summarize_messages = [
        {
            "role": "system",
            "content": (
                "You are a helpful food ordering assistant. "
                "Live data from Swiggy and Zomato has already been retrieved. "
                "Your ONLY job is to read the data provided and write a clear, helpful answer. "
                "Do NOT call any tools. Do NOT say you will search. Just read the data and answer."
            )
        },
        {
            "role": "user",
            "content": (
                "The user asked: " + user_question + "\n\n"
                "Here is the live data retrieved from the food platforms:\n\n"
                + tool_results +
                "\n\nNow write a complete, well-formatted answer based strictly on this data. "
                "Include restaurant names, ratings, prices, delivery charges if shown, "
                "and which platform is better value. Be specific with numbers."
            )
        }
    ]

    try:
        resp = client.chat(
            model=_current_model,
            messages=summarize_messages,
            options={"temperature": 0, "num_predict": 2048},
        )
        return _strip_think(resp.message.content or "").strip()
    except Exception as e:
        return "[Summarization error: " + str(e) + "]"

# ─────────────────────────────────────────
#  HISTORY SUMMARIZATION  ← NEW
# ─────────────────────────────────────────
def _summarize_history(client, history: list[dict]) -> list[dict]:
    """
    Compress old turns into a single summary message to save context space.
    Always keeps the last 2 turns (4 messages) verbatim for sharp short-term memory.
    Triggered when history reaches 16+ messages (8+ turns).
    """
    if len(history) < 6:
        return history

    to_summarize = history[:-4]   # everything except last 2 turns
    keep_recent  = history[-4:]   # last 2 turns kept verbatim

    convo_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}"
        for m in to_summarize
        if m.get("content")
    )

    console.print("  [dim yellow]📝 Summarizing old history to save context...[/]")
    try:
        resp = client.chat(
            model=_current_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize the following food ordering conversation concisely. "
                        "Preserve key facts: dishes searched, restaurants found, prices quoted, "
                        "addresses used, orders placed, coupons applied, and any user preferences."
                    )
                },
                {"role": "user", "content": convo_text}
            ],
            options={"temperature": 0, "num_predict": 512},
        )
        summary = _strip_think(resp.message.content or "").strip()
    except Exception as e:
        summary = f"[Earlier conversation — summary failed: {e}]"

    # Inject summary as a synthetic exchange so the model treats it naturally
    summary_block = [
        {
            "role":    "user",
            "content": f"[CONVERSATION SUMMARY — earlier context]\n{summary}"
        },
        {
            "role":    "assistant",
            "content": "Understood, I have context from our earlier conversation."
        }
    ]
    return summary_block + keep_recent


def run_agent(user_message: str, history: list[dict]) -> str:
    """
    Native Ollama SDK agent loop optimised for gpt-oss:20b.

    gpt-oss:20b goes silent (empty response) after receiving large tool results.
    Fix: run tool-calling phase normally, then call the model FRESH with tool
    data injected as plain text for summarization.
    """
    client = ollama_sdk.Client(host=OLLAMA_BASE_URL)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += history[-20:]   # ← up to 10 turns (20 messages)
    messages.append({"role": "user", "content": user_message})

    call_log: set[str] = set()
    think_retries = 0
    tool_results_collected: list[str] = []

    for round_num in range(1, MAX_ROUNDS + 1):
        console.print(f"  [dim]🔄 Round {round_num}/{MAX_ROUNDS}...[/]")

        try:
            resp = client.chat(
                model=_current_model,
                messages=messages,
                tools=OLLAMA_TOOLS,
                options={"temperature": 0, "num_predict": 2048},
            )
        except Exception as e:
            return f"[Ollama error: {e}]"

        msg         = resp.message
        raw_content = msg.content or ""
        content     = _strip_think(raw_content)

        # Detect silent thinking — gpt-oss:20b emits only <think> tags with no action
        thinking_only = (
            not msg.tool_calls
            and not content.strip()
            and "<think>" in raw_content
        )
        if thinking_only:
            think_retries += 1
            console.print(f"  [dim yellow]💭 Model is thinking... nudge {think_retries}/2[/]")
            if think_retries <= 2:
                messages.append({
                    "role": "user",
                    "content": "Stop thinking. Call the appropriate tool right now to answer the user question."
                })
                continue
            # Two nudges failed — force summarize or give up
            if tool_results_collected:
                console.print("  [dim yellow]⚠️  Forcing summarization from collected data...[/]")
                combined = "\n\n".join(tool_results_collected)
                return _summarize_with_fresh_call(client, user_message, combined)
            return "[Model stuck in thinking loop. Try: search biryani on zomato]"

        # No tool calls = model is done with tool phase
        if not msg.tool_calls:
            if content.strip():
                return content
            # Empty with no thinking = model stalled after tool results (main gpt-oss:20b bug)
            if tool_results_collected:
                console.print("  [dim yellow]⚠️  Empty reply after tools — fresh summarization...[/]")
                combined = "\n\n".join(tool_results_collected)
                return _summarize_with_fresh_call(client, user_message, combined)
            return "[Empty response. Try: search biryani on zomato]"

        # Append assistant turn with tool calls
        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": msg.tool_calls,
        })

        # Execute each tool call
        for tc in msg.tool_calls:
            name = tc.function.name
            args = tc.function.arguments or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}

            dedup_key = f"{name}:{json.dumps(args, sort_keys=True)}"
            if dedup_key in call_log:
                console.print(f"  [yellow]⚠️  Duplicate skipped: {name}[/]")
                result = f"[{name} already called with same args — skipped]"
            else:
                call_log.add(dedup_key)
                handler = TOOL_HANDLERS.get(name)
                result  = handler(args) if handler else f"[Unknown tool: {name}]"

            messages.append({"role": "tool", "content": result, "name": name})
            tool_results_collected.append(f"[{name}]\n{result}")

        # After round 4+ with data collected, stop and summarize
        # gpt-oss:20b doesn't reliably produce output after round 3
        if round_num >= 4 and tool_results_collected:
            console.print("  [dim]✅ Data collected — summarizing with fresh call...[/]")
            combined = "\n\n".join(tool_results_collected)
            return _summarize_with_fresh_call(client, user_message, combined)

    # Final fallback
    if tool_results_collected:
        combined = "\n\n".join(tool_results_collected)
        return _summarize_with_fresh_call(client, user_message, combined)
    return "[No data collected — try: search biryani on zomato]"


# ─────────────────────────────────────────
#  UI HELPERS
# ─────────────────────────────────────────
def print_banner():
    console.print()
    console.print(Panel.fit(
        "[bold cyan]🍕  FoodBot[/]  ·  [green]Swiggy[/] + [red]Zomato[/]  ·  CLI\n"
        "[dim]Powered by Ollama · MCP  (100% local AI)[/]",
        border_style="cyan", padding=(0, 4)
    ))
    console.print()

def show_status():
    ollama_ok = _check_ollama_running()
    t = Table(box=box.ROUNDED, border_style="dim", show_header=False, padding=(0,2))
    t.add_column(style="bold"); t.add_column()
    t.add_row("Zomato",     "[green]✅ logged in[/]"  if TOKENS["zomato"]      else "[red]❌ login zomato[/]")
    t.add_row("Swiggy",     "[green]✅ logged in[/]"  if TOKENS["swiggy_food"] else "[red]❌ login swiggy[/]")
    t.add_row("Ollama",     f"[green]✅ {_current_model}[/]" if ollama_ok      else "[red]❌ ollama serve[/]")
    t.add_row("Max rounds", f"[dim]{MAX_ROUNDS} tool rounds per query[/]")
    t.add_row("History",    "[dim]10 turns · auto-summarized at 8+[/]")
    console.print(Panel(t, title="[bold]Status[/]", border_style="dim", padding=(0,1)))
    console.print()

def print_reply(text: str):
    console.print()
    console.print(Panel(text, title="[bold green]🤖 FoodBot[/]", border_style="green", padding=(0,2)))
    console.print()

def print_error(msg: str):
    console.print(Panel(f"[red]{escape(msg)}[/]", title="[bold red]Error[/]", border_style="red", padding=(0,2)))

def debug_auth():
    console.print(Rule("[bold]mcp-remote Cache[/]"))
    if not MCP_AUTH_DIR.exists():
        console.print("[red]~/.mcp-auth/ does not exist[/]"); return
    for f in sorted(MCP_AUTH_DIR.rglob("*")):
        if not f.is_file(): continue
        console.print(f"\n[bold cyan]{f.relative_to(Path.home())}[/]")
        try:
            parsed = json.loads(f.read_text())
            for k, v in parsed.items():
                val = (v[:40]+"…") if isinstance(v,str) and len(v)>40 else str(v)
                console.print(f"  [dim]{k}:[/] {val}")
        except: console.print(f"  [dim]{f.read_text()[:200]}[/]")
    console.print()

HELP_TEXT = """
[bold cyan]LOGIN[/]
  [green]login zomato[/]  /  [green]login swiggy[/]
  [green]login-manual zomato[/] [dim]<token>[/]  /  [green]login-manual swiggy[/] [dim]<token>[/]

[bold cyan]OLLAMA[/]
  [green]models[/]        list installed models
  [green]model[/] [dim]<n>[/]    switch model (e.g. model gpt-oss:20b)

[bold cyan]OTHER[/]
  [green]status[/]  [green]debug-auth[/]  [green]reload[/]  [green]logout[/]  [green]help[/]  [green]quit[/]
  [green]tools[/] [dim]zomato|swiggy|instamart|dineout[/]

[bold cyan]EXAMPLE QUERIES[/]
  search hyderabadi biryani on zomato
  compare biryani on swiggy and zomato
  show menu for Behrouz Biryani
  track my last order
  search milk eggs on instamart
  book a table for 2 tonight
"""

def run_mcp_remote_login(platform: str):
    url = MCP_LOGIN_URLS.get(platform)
    if not url: console.print(f"[red]Unknown: {platform}[/]"); return
    console.print(Panel(f"[bold]Login to {platform.title()}[/]\n[dim]Press Ctrl+C when done[/]",
                        title=f"🔐 {platform.title()}", border_style="cyan", padding=(0,2)))
    try: subprocess.run(["npx", "mcp-remote", url])
    except FileNotFoundError:
        console.print("[red]npx not found — install Node.js: https://nodejs.org/en/download[/]"); return
    except KeyboardInterrupt: pass
    if platform == "zomato": TOKENS["zomato"] = ""
    else:
        for k in SWIGGY_KEYS: TOKENS[k] = ""
    if load_tokens(verbose=True): save_tokens(); show_status()

# ─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────
def main():
    global _current_model, OLLAMA_MODEL

    print_banner()
    load_tokens(verbose=True)
    show_status()

    if not _check_ollama_running():
        console.print(Panel(
            "[yellow]Ollama is not running![/]\n\nStart with: [bold green]ollama serve[/]",
            title="[red]Ollama Offline[/]", border_style="red", padding=(0,2)
        ))

    console.print(f"  [dim]🧠 Model: [bold]{_current_model}[/] · think-tokens stripped · dedup + round-limit active[/]")
    console.print("  [green]✅ Ready![/]  Type [bold]help[/] for commands.\n")

    session  = PromptSession()
    pt_style = PTStyle.from_dict({"prompt": "ansicyan bold", "": "ansiwhite"})
    history: list[dict] = []

    while True:
        try:
            line = session.prompt(HTML("<prompt>❯ </prompt>"), style=pt_style).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye! 👋[/]\n"); break

        if not line: continue
        cmd = line.lower().strip()

        if cmd in ("quit","exit","q"):
            console.print("[dim]Goodbye! 👋[/]\n"); break
        elif cmd == "help":
            console.print(Panel(HELP_TEXT, title="[bold cyan]Help[/]", border_style="cyan", padding=(0,2)))
        elif cmd == "status":
            show_status()
        elif cmd == "debug-auth":
            debug_auth()
        elif cmd == "logout":
            clear_tokens()
        elif cmd == "reload":
            for k in TOKENS: TOKENS[k] = ""
            load_tokens(verbose=True); show_status()
        elif cmd == "login zomato":
            run_mcp_remote_login("zomato")
        elif cmd == "login swiggy":
            run_mcp_remote_login("swiggy")
        elif cmd == "models":
            installed = _list_ollama_models()
            if not installed:
                console.print("[yellow]No models or Ollama not running.[/]")
            else:
                t = Table(box=box.ROUNDED, border_style="dim", padding=(0,2))
                t.add_column("[bold]Model[/]", style="cyan"); t.add_column("[bold]Status[/]")
                for m in installed:
                    t.add_row(m, "[green]⭐ active[/]" if m == _current_model else "[dim]installed[/]")
                console.print(Panel(t, title="[bold]🦙 Models[/]", border_style="dim"))
                console.print("  [dim]Switch: model <name>[/]\n")
        elif cmd.startswith("model "):
            _current_model = line.split(maxsplit=1)[1].strip()
            OLLAMA_MODEL = _current_model
            console.print(f"  [green]✅ Switched to: {_current_model}[/]")
            show_status()
        elif cmd.startswith("login-manual "):
            parts = line.split(maxsplit=2)
            if len(parts) != 3:
                console.print("[yellow]Usage: login-manual zomato <token>[/]"); continue
            p, tok = parts[1].lower(), parts[2].strip()
            if p == "zomato": TOKENS["zomato"] = tok
            elif p == "swiggy": _set_swiggy(tok)
            else: console.print("[red]Platform must be zomato or swiggy[/]"); continue
            save_tokens(); show_status()
        elif cmd == "tools" or cmd.startswith("tools "):
            parts = cmd.split(maxsplit=1)
            if len(parts) == 1:
                console.print("[yellow]Usage: tools zomato | swiggy | instamart | dineout[/]")
            else:
                km = {"zomato":"zomato","swiggy":"swiggy_food","instamart":"swiggy_instamart","dineout":"swiggy_dineout"}
                key = km.get(parts[1].strip())
                if key:
                    t = Table(box=box.ROUNDED, border_style="dim", padding=(0,1))
                    t.add_column("[bold]Tool[/]", style="cyan"); t.add_column("[bold]Description[/]")
                    with console.status("[dim]Fetching...[/]", spinner="dots"):
                        tools = _run_async(_mcp_list_tools(key))
                    for name, desc in tools: t.add_row(name, desc)
                    console.print(Panel(t, title=f"[bold]📦 {parts[1].title()} Tools[/]", border_style="dim"))
        else:
            # ── AI query ──────────────────────────────────────
            try:
                console.print(f"  [dim cyan]⠋ Thinking with {_current_model}...[/]")
                reply = run_agent(line, history)

                if reply and reply.strip():
                    history.append({"role": "user",     "content": line})
                    history.append({"role": "assistant", "content": reply})

                    # ── Summarize old history when it gets long ── ← NEW
                    if len(history) >= 16:
                        client = ollama_sdk.Client(host=OLLAMA_BASE_URL)
                        history = _summarize_history(client, history)

                    history = history[-20:]   # hard cap: 10 turns (20 messages)
                    print_reply(reply)
                else:
                    console.print(Panel(
                        "[yellow]⚠️  Empty response from model.[/]\n\n"
                        "[dim]Try:[/]  [green]search biryani on zomato[/]",
                        title="[bold yellow]No Response[/]", border_style="yellow", padding=(0,2)
                    ))
            except Exception as e:
                print_error(str(e))
                if "connect" in str(e).lower():
                    console.print("  [yellow]💡 Run: [bold]ollama serve[/][/]")

if __name__ == "__main__":
    main()
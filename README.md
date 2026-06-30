# 🍕 FoodBot CLI

A terminal-based AI food ordering assistant that talks to **Swiggy** and **Zomato** through their MCP (Model Context Protocol) servers, powered by a **100% local LLM** via Ollama. Search restaurants, compare prices across platforms, browse menus, manage carts, track orders, and book tables — all from natural language, all running locally.

```
❯ compare biryani on swiggy and zomato
  🔄 Round 1/8...
  ⚡ calling get_saved_addresses_for_user...
  ⚡ calling get_addresses...
  ⚡ compare_prices: searching both platforms for 'biryani'...

🤖 FoodBot
  Behrouz Biryani — ⭐ 4.3
  Swiggy: ₹320 + ₹25 delivery + ₹18 tax = ₹363
  Zomato: ₹310 + ₹30 delivery + ₹17 tax = ₹357
  → Zomato is cheaper by ₹6
```

## Why This Exists

Most "AI shopping agent" demos call out to a hosted LLM API and ship your data to a third party. FoodBot runs entirely against a **local Ollama model** (default: `gpt-oss:20b`) — no order data, addresses, or browsing history ever leaves your machine except to talk directly to Swiggy/Zomato themselves.

## Key Features

### Cross-platform price comparison
A single `compare_prices` tool call fetches results from Swiggy and Zomato in parallel for the same dish, instead of forcing the model to run two sequential searches and stitch them together itself. The system prompt explicitly instructs the model to fetch both platforms' saved addresses first, then call `compare_prices` once with both address IDs — preventing wasted tool rounds.

### Natural language ordering, no command syntax
There's no rigid `/order <restaurant> <item>` syntax. Plain sentences like *"find me a cheap biryani place nearby"* or *"add 2 of the first item to cart"* are parsed by the LLM into the correct tool calls with the correct arguments.

### Full order lifecycle, four platforms
| Platform | Capabilities |
|---|---|
| Zomato | Address lookup, restaurant search, menu by listing or category, cart creation, coupon/offer lookup, checkout, order tracking, order history |
| Swiggy Food | Address lookup, restaurant + menu search, full menu browsing (paginated), cart view/update/clear, coupon fetch + apply, COD order placement (≤₹999), active orders, order tracking, order details |
| Swiggy Instamart | Grocery product search, category browsing, add-to-cart, cart view, order placement |
| Swiggy Dineout | Restaurant search by city/cuisine, offers lookup, table booking (date/time/guest count) |

That's **29 distinct tools** wired up, each with its own Ollama function-calling schema (see `OLLAMA_TOOLS`) and a dedicated handler function (see `TOOL_HANDLERS`).

### Human-in-the-loop safety
Every tool call goes through `tool_confirm()` before execution, rendering the server, tool name, and full argument list in a panel so you can see exactly what's about to run — especially important for anything that touches your cart, places an order, or books a table. Four choices are offered per call:

1. **Allow once** — confirm just this call
2. **Always allow this tool** — skip confirmation for this tool name for the rest of the session
3. **Allow all** — skip confirmation entirely for the rest of the session
4. **Skip** — refuse the call; the model receives `[Skipped by user]` as the result and has to adapt

Confirmation state is thread-safe (`_confirm_lock`) since MCP calls run on a background asyncio event loop separate from the main input thread.

### 100% local inference
No OpenAI/Anthropic/Gemini API key required anywhere in this codebase. The only network calls are to (a) your local Ollama daemon on `localhost:11434`, and (b) the Swiggy/Zomato MCP servers themselves to fetch your own account data. There's no per-token billing and no cloud LLM provider in the loop.

### Built to survive a flaky local model
`gpt-oss:20b` running locally has a well-documented failure mode: after receiving a large tool result, it sometimes emits an empty response, or gets stuck producing only `<think>...</think>` reasoning tokens with no actual output or tool call. FoodBot handles this with a layered fallback:

1. **Think-token stripping** — `<think>` blocks are regex-stripped from every response before being shown or stored
2. **Nudging** — if the model produces only thinking with no action, it gets up to 2 explicit "stop thinking, call the tool now" nudges
3. **Forced summarization** — if nudging fails, or if the model goes empty after tool results come back, FoodBot starts a **fresh, tool-free chat thread**, hands the model the raw tool data as plain text, and asks it to just write an answer — bypassing whatever state caused it to stall in the original thread
4. **Round capping** — after round 4 of tool calls (of a max 8), if any data has been collected, FoodBot proactively stops and summarizes rather than risking another stall

### Context-aware conversation with auto-summarization
Conversation history is capped at the last 10 turns (20 messages) sent to the model per request. Once history reaches 16+ messages, older turns are compressed: everything except the last 2 turns is sent to the model in a dedicated summarization call that's instructed to preserve dishes searched, restaurants found, prices quoted, addresses used, orders placed, and coupons applied — then injected back into history as a synthetic user/assistant exchange so the model treats it as natural prior context rather than a meta-instruction.

### Tool-call deduplication
Every tool call is hashed by `name + sorted(args)` and tracked in a per-request `call_log` set. If the model tries to call the same tool with identical arguments twice in one query (a known LLM tendency when uncertain), the second call is skipped and the model is told it already has that result — preventing redundant API calls and confirmation prompts.

### Rich terminal UI
Built on `rich` (panels, tables, styled text) and `prompt_toolkit` (history-aware prompt, custom styling) rather than plain `print()`/`input()` — status displays, tool confirmation panels, and replies all render as bordered, color-coded blocks.

## Architecture

```
User input (CLI)
      │
      ▼
Ollama (local LLM, tool-calling enabled)
      │
      ▼
Tool dispatch ──► MCP client (streamable HTTP) ──► Zomato MCP server
                                               └──► Swiggy Food MCP server
                                               └──► Swiggy Instamart MCP server
                                               └──► Swiggy Dineout MCP server
      │
      ▼
Tool results ──► fresh summarization call ──► formatted reply
```

Each tool call is confirmed by the user, deduplicated within a session, and capped at `MAX_ROUNDS` (default 8) to prevent runaway agent loops. After tool data is collected, FoodBot issues a **fresh, tool-free call** to the model purely to summarize results — this works around `gpt-oss:20b` stalling when asked to both call tools and write prose in the same turn.

## Prerequisites

- Python 3.10+
- [Ollama](https://ollama.com) installed and running locally, with a tool-calling-capable model pulled (e.g. `ollama pull gpt-oss:20b`)
- [Node.js](https://nodejs.org) (for `npx mcp-remote`, used during OAuth login to Swiggy/Zomato)
- Active Swiggy and/or Zomato accounts

## Installation

```bash
git clone https://github.com/yaswanthreddy3/foodbot-cli.git
cd foodbot-cli
pip install -r requirements.txt
```

Start Ollama (in a separate terminal):
```bash
ollama serve
ollama pull gpt-oss:20b
```

Optionally configure via `.env`:
```bash
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=gpt-oss:20b
```

## Usage

```bash
python chatbot.py
```

On first run, log in to each platform:
```
❯ login zomato
❯ login swiggy
```
This opens an OAuth flow via `mcp-remote`; tokens are cached locally (`~/.mcp-auth/`, `tokens.json`) and never committed to the repo.

### Example queries
```
search hyderabadi biryani on zomato
compare biryani on swiggy and zomato
show menu for Behrouz Biryani
track my last order
search milk eggs on instamart
book a table for 2 tonight
```

### Commands
| Command | Description |
|---|---|
| `login zomato` / `login swiggy` | OAuth login via mcp-remote |
| `login-manual <platform> <token>` | Manually set a token |
| `status` | Show login state, active model, session config |
| `models` | List installed Ollama models |
| `model <name>` | Switch active model |
| `tools <platform>` | List available MCP tools for a platform |
| `debug-auth` | Inspect cached mcp-remote token files |
| `reload` | Reload tokens from disk |
| `logout` | Clear all cached tokens |
| `help` | Show command reference |
| `quit` | Exit |

## Tech Stack

`Python` · `Ollama` (local LLM inference) · `MCP` (Model Context Protocol, streamable HTTP) · `rich` (terminal UI) · `prompt_toolkit` (interactive input) · `mcp-remote` / Node.js (OAuth bridge)

## Security Notes

- Auth tokens are stored locally only, in `tokens.json` (chmod `600`) and `~/.mcp-auth/`, both excluded via `.gitignore`
- Every state-changing tool call (cart updates, checkout, order placement, table booking) requires explicit user confirmation before execution
- No order data, search history, or personal info is sent anywhere except directly to Swiggy/Zomato's own MCP endpoints and your local Ollama instance

## Known Limitations

- `gpt-oss:20b` occasionally requires the silent-thinking workaround described above; smaller/faster models may behave more predictably but with less reasoning quality
- Swiggy order placement is currently COD-only, capped at ₹999 per the underlying MCP tool
- Requires Node.js purely for the `mcp-remote` OAuth bridge during login

## Troubleshooting

**"Ollama is not running" on startup**
Run `ollama serve` in a separate terminal, then restart FoodBot or use the `reload` command.

**Empty / blank responses from the model**
This is the known `gpt-oss:20b` stall described above. FoodBot should auto-recover via fresh summarization, but if you still see `[Empty response. Try: search biryani on zomato]`, try rephrasing the query more directly, or switch to a smaller/faster model with `model <name>` to compare behavior.

**"Not logged in to zomato/swiggy"**
Run `login zomato` or `login swiggy`. If the browser-based OAuth flow doesn't complete, check that Node.js and `npx` are installed (`node -v`), and that `~/.mcp-auth/` is writable.

**`npx not found`**
Install Node.js from https://nodejs.org — `mcp-remote` is only needed for the OAuth login step, not for normal operation afterward.

**Tool call seems stuck / hangs**
Each MCP call has a 30-second timeout (`_run_async`). If it times out, you'll see `[Timeout — MCP call took too long]` as the result and the agent loop continues rather than hanging indefinitely.

**Want to see exactly what tokens are cached**
Run `debug-auth` to inspect `~/.mcp-auth/` contents directly in the terminal.

## Project Structure

```
foodbot-cli/
├── chatbot.py          # Entire application: MCP client, agent loop, CLI, handlers
├── requirements.txt     # Python dependencies
├── .env                 # Optional: OLLAMA_BASE_URL, OLLAMA_MODEL (gitignored)
├── .gitignore
└── README.md
```

The project is intentionally kept as a single file for now — the agent loop, tool handlers, and CLI are all in `chatbot.py`. A natural next step would be splitting this into `mcp_client.py`, `tools.py`, `agent.py`, and `cli.py` modules as functionality grows.

## Roadmap / Possible Extensions

- Split the single-file script into modules for maintainability
- Add automated tests for the dedup logic, history summarization, and think-token stripping
- Support additional payment methods for Swiggy orders beyond COD
- Add a `--model` CLI flag to set the model without entering the interactive session first
- Persist conversation history across sessions (currently in-memory only, reset on exit)
- Optional structured JSON logging of tool calls for debugging/audit trail

## License

MIT
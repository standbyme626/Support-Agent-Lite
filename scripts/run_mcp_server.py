"""C7 stdio entrypoint — Claude Desktop / MCP inspector compatible.

Run:
    .venv/bin/python scripts/run_mcp_server.py

Claude Desktop config (claude_desktop_config.json):
{
  "mcpServers": {
    "support-agent-lite": {
      "command": "/abs/path/to/.venv/bin/python",
      "args": ["/abs/path/to/scripts/run_mcp_server.py"]
    }
  }
}
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

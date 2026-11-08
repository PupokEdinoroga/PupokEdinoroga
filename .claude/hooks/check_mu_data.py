"""PostToolUse: после правки murdrum-bot/tools/*.py — компиляция и тесты mu_data.

Код выхода 2 + текст в stderr возвращает ошибку Claude, чтобы он её исправил.
"""
import json
import os
import subprocess
import sys

data = json.load(sys.stdin)
path = (data.get("tool_input") or {}).get("file_path", "").replace("\\", "/")
if "murdrum-bot/tools/" not in path or not path.endswith(".py"):
    sys.exit(0)

root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
tools = os.path.join(root, "murdrum-bot", "tools")

r = subprocess.run([sys.executable, "-m", "py_compile", path], capture_output=True, text=True)
if r.returncode:
    print(f"py_compile не прошёл для {path}:\n{r.stderr}", file=sys.stderr)
    sys.exit(2)

r = subprocess.run([sys.executable, "test_mu_data.py"], cwd=tools, capture_output=True, text=True, timeout=120)
if r.returncode:
    print(f"Тесты mu_data упали:\n{r.stdout[-2000:]}\n{r.stderr[-3000:]}", file=sys.stderr)
    sys.exit(2)

"""PreToolUse (Bash): не даёт закоммитить murdrum-bot/settings.ini с заполненными Login/Password.

Проверяется версия файла в индексе (то, что реально попадёт в коммит),
а для `git commit -a` / `--all` — рабочая копия.
"""
import json
import os
import re
import subprocess
import sys

data = json.load(sys.stdin)
cmd = (data.get("tool_input") or {}).get("command", "")
if not re.search(r"\bgit\b[^\n;&|]*\bcommit\b", cmd):
    sys.exit(0)

root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
rel = "murdrum-bot/settings.ini"
texts = []
r = subprocess.run(["git", "show", f":{rel}"], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
if r.returncode == 0:
    texts.append(r.stdout)
if re.search(r"\s(-a|--all|-am)\b", cmd):
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as f:
            texts.append(f.read())
    except OSError:
        pass

for text in texts:
    for key in ("Login", "Password"):
        m = re.search(rf"^[ \t]*{key}[ \t]*=[ \t]*(\S.*)$", text, re.M | re.I)
        if m:
            print(f"Коммит заблокирован: в {rel} заполнено {key}=. "
                  f"Очисти значение (оставь '{key}=') перед коммитом — пароль не должен попасть в git.",
                  file=sys.stderr)
            sys.exit(2)

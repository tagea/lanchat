"""Подставляет номер версии сборки в lanchat.py (вызывается на GitHub перед сборкой)."""
import re
import sys

version = sys.argv[1]
path = "lanchat.py"
src = open(path, encoding="utf-8").read()
src, n = re.subn(r'^APP_VERSION = ".*"$', f'APP_VERSION = "{version}"', src, count=1, flags=re.M)
assert n == 1, "APP_VERSION not found"
open(path, "w", encoding="utf-8").write(src)
print("version", version)

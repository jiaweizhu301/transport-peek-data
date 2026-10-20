#!/usr/bin/env python3
"""SEC-04 / HY-07 本地守门：供应链钉死，纯离线、只读文件。

断言：
  1. 每个 workflow 的每个 `uses:` 都钉在 40 位 commit SHA 上，并带 `# vX.Y.Z` 注释（Dependabot 靠它升级）。
  2. daily.yml 装依赖只走 `pip install --require-hashes -r scripts/requirements.txt`，不许裸 `pip install 包名`。
  3. scripts/requirements.txt 每个包都 `==` 钉版本且至少一个 `--hash=sha256:`（含 rpds-py 的 cp312 manylinux x86_64 轮子）。
  4. dependabot.yml 存在，且覆盖 github-actions 与 pip 两个生态。

两种布局都认：data 仓（.github/、scripts/）与主仓 pipeline/（github/、scripts/）。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GH = next((os.path.join(ROOT, d) for d in (".github", "github") if os.path.isdir(os.path.join(ROOT, d))), None)

USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)(.*)$")
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"#\s*v\d+\.\d+\.\d+")
REQ = re.compile(r"^([A-Za-z0-9_.-]+)==([\w.]+)")


def check(gh_dir, scripts_dir):
    errors = []
    if not gh_dir:
        return ["没找到 .github/ 或 github/ 目录"]
    wf_dir = os.path.join(gh_dir, "workflows")
    workflows = sorted(f for f in os.listdir(wf_dir) if f.endswith((".yml", ".yaml")))
    for name in workflows:
        for n, line in enumerate(open(os.path.join(wf_dir, name), encoding="utf-8"), 1):
            m = USES.match(line)
            if not m:
                continue
            ref, rest = m.group(1), m.group(2)
            if ref.startswith("./"):
                continue
            if not PINNED.match(ref):
                errors.append(f"{name}:{n} action 没钉 SHA：{ref}")
            elif not VERSION_COMMENT.search(rest):
                errors.append(f"{name}:{n} 缺 `# vX.Y.Z` 注释：{ref}")
        text = open(os.path.join(wf_dir, name), encoding="utf-8").read()
        for n, line in enumerate(text.splitlines(), 1):
            if re.search(r"\bpip3?\s+install\b", line) and "--require-hashes" not in line:
                errors.append(f"{name}:{n} pip install 没带 --require-hashes：{line.strip()}")
    daily = os.path.join(wf_dir, "daily.yml")
    if os.path.exists(daily):
        lines = open(daily, encoding="utf-8").read().splitlines()
        if not any("pip install" in ln and "--require-hashes" in ln and "-r scripts/requirements.txt" in ln for ln in lines):
            errors.append("daily.yml 没有 `pip install --require-hashes -r scripts/requirements.txt`")

    req = os.path.join(scripts_dir, "requirements.txt")
    if not os.path.exists(req):
        errors.append("scripts/requirements.txt 不存在")
    else:
        # 续行（\\ 结尾）拼成逻辑行
        logical, buf = [], ""
        for raw in open(req, encoding="utf-8"):
            s = raw.strip()
            if not s or (s.startswith("#") and not buf):
                continue
            if buf and REQ.match(s):  # 上一包的 hash 行被删、只剩悬空续行符：别把下一包的 hash 算给它
                logical.append(buf.strip())
                buf = ""
            buf += " " + s.rstrip("\\").strip()
            if not s.endswith("\\"):
                logical.append(buf.strip())
                buf = ""
        if buf:
            logical.append(buf.strip())
        names = set()
        for entry in logical:
            m = REQ.match(entry)
            if not m:
                errors.append(f"requirements.txt 没用 == 钉版本：{entry[:60]}")
                continue
            names.add(m.group(1).lower().replace("_", "-"))
            if not re.search(r"--hash=sha256:[0-9a-f]{64}", entry):
                errors.append(f"requirements.txt {m.group(1)} 没有 --hash=sha256")
        for need in ("jsonschema", "attrs", "jsonschema-specifications", "referencing", "rpds-py"):
            if need not in names:
                errors.append(f"requirements.txt 缺 {need}")

    dep = os.path.join(gh_dir, "dependabot.yml")
    if not os.path.exists(dep):
        errors.append("dependabot.yml 不存在")
    else:
        t = open(dep, encoding="utf-8").read()
        for eco in ("github-actions", "pip"):
            if not re.search(r'package-ecosystem:\s*["\']?%s["\']?\s*$' % re.escape(eco), t, re.M):
                errors.append(f"dependabot.yml 没覆盖 {eco}")
    return errors


def main():
    errors = check(GH, HERE)
    for e in errors:
        print("FAIL", e)
    if errors:
        print(f"supply_chain_check: {len(errors)} 处不合格")
        return 1
    print("supply_chain_check: OK（actions 钉 SHA、pip 带 hash、Dependabot 在）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""reindex —— mpkg 注册表**确定性重建器**（index.json 由目录内容推导，不人工记账）。

为什么需要它（血训，2026-09-27 实测审计）：
  `mpkg.py publish` 的写入是「先传包文件 → 再读改写 index.json」两步，且
  `attestations` 计数是**冗余存储**（回执文件在 attestations/ 里，计数却在 index 里）。
  冗余 = 会漂移。实测：
    - asr-code-rust-book@0.1.0  index 记 attestations=1，盘上真实回执 = 2
    - kbv-video-distill@0.1.0   盘上有 2 个 id 的构建，index 只列 1 个（另一个静默不可见）

  解法：**index = f(packages/, attestations/)**。计数实算、身份实算、顺序确定。
  人只做一件事：把包文件放进来 / 删出去（下架协议，见 README）。

契约对齐（lystack/proto/mpkg/GOLDEN.md §6「新实现验收标准」）：
  自带 GOLDEN 三例自检（canon 幂等 + content-id 逐字节），**自检不过就拒绝写盘** ——
  将来 canon 规则若改，本工具会在写坏 index 之前先失败，而不是悄悄算出另一个 id。

用法:
  python tools/reindex.py --check     # 只校验（CI 门禁；不一致退出码 2）
  python tools/reindex.py             # 重建 index.json（就地覆盖，人工 review 后提交）
  python tools/reindex.py --registry D:/Code/mpkg-registry
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile

MANIFEST = "mpkg.json"
REQUIRED = ("mpkg", "name", "version", "intent", "steps", "verify")
FORMAT = "0.1"

# ── 契约源自检向量 ────────────────────────────────────────────────────────────
# 每项 = (canon_json 精确字节, content_id)。取自 lystack/proto/mpkg/golden/cases.json。
# 校验方式：canon(json.loads(canon_json)) 必须**逐字节等于** canon_json（canon 是幂等范式），
# 且 sha256(canon_json) 必须等于 content_id。这样 canon 规则与哈希层同时被钉住。
GOLDEN: list[tuple[str, str]] = [
    (
        '{"manifest":{"intent":"最小合法包：仅 manifest，无 files 键","mpkg":"0.1","name":"hello-mpkg",'
        '"steps":[{"run":"echo hello-mpkg"}],"verify":["true"],"version":"0.1.0"}}',
        "sha256:830d8d92aa3a7ca9655468c5fc034e10902f9aad225825fdb7e88824a7eeda58",
    ),
    (
        '{"files":{"README.md":"fa92edd9d1241161b07fce3404e6c8cceb23dd8790521e274923db0afdc14f14",'
        '"artifacts/lib/util.py":"b1b3395bd5ce1ca48ec48abae5c25b1007bd4b0762717b811742579f93fe1a83",'
        '"artifacts/main.py":"e1f295db15fc563c98997b625f807baf10d885df64a8ab5a4272cde6401afbd2"},'
        '"manifest":{"intent":"含 files 的多文件包：嵌套路径哈希引用","mpkg":"0.1","name":"multi-file-demo",'
        '"steps":[{"expect":{"exit":0},"run":"echo multi"}],'
        '"verify":["test -f artifacts/main.py","test -f artifacts/lib/util.py"],"version":"0.2.0"}}',
        "sha256:f501b4282a8dcb3d44b3a1978ac0643f280fa5926e131e3f087204e7355febd7",
    ),
    (
        '{"files":{"artifacts/out.txt":"084c799cd551dd1d8d5c5f9a5d593b2e931f5e36122ee5c793c1d08a19839cc0"},'
        '"manifest":{"author":"agent:lilyco",'
        '"intent":"步骤齐全包：expect.exit / 多 verify / requirements / 溯源可选字段","license":"MIT",'
        '"mpkg":"0.1","name":"full-replay-suite",'
        '"requirements":{"tools":[{"min_version":"3.10","name":"python3"}]},'
        '"steps":[{"run":"echo step-1"},{"expect":{"exit":0},"run":"python3 -c \'print(6*7)\'"},'
        '{"expect":{"exit":0},"run":"test -d {{work}}"}]'
        ',"tags":["demo","replay"],'
        '"verify":["test -f artifacts/out.txt","grep -q 42 artifacts/out.txt"],"version":"1.0.0"}}',
        "sha256:7bc60dd2e45e99cdd8b095f36b28234899495743001001d2c1169963bcf55086",
    ),
]


def canon(obj) -> str:
    """GOLDEN.md §2：递归键排序 + 紧凑分隔符 + UTF-8 原样。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def self_test() -> list[str]:
    """契约源自检。返回错误列表（空 = 通过）。"""
    errs = []
    for i, (want_canon, want_id) in enumerate(GOLDEN, 1):
        got_canon = canon(json.loads(want_canon))
        if got_canon != want_canon:
            errs.append(f"golden#{i} canon 非幂等\n  want {want_canon}\n  got  {got_canon}")
        got_id = "sha256:" + hashlib.sha256(want_canon.encode("utf-8")).hexdigest()
        if got_id != want_id:
            errs.append(f"golden#{i} content_id 不符: want {want_id} got {got_id}")
    return errs


def content_id(pack: dict) -> str:
    """content-id = 'sha256:' + sha256(canon(pack) 的 UTF-8 字节)。pack = {manifest, files?}。"""
    return "sha256:" + hashlib.sha256(canon(pack).encode("utf-8")).hexdigest()


def validate(m: dict) -> str | None:
    """返回错误信息；None = 合法。判据比 mpkg.py 略严（name 必须小写 kebab-case，防路径穿越）。"""
    for k in REQUIRED:
        if k not in m:
            return f"{MANIFEST} 缺必需字段: {k}"
    if m.get("mpkg") != FORMAT:
        return f"不支持的 mpkg 版本: {m.get('mpkg')!r}（期望 {FORMAT!r}）"
    name = m.get("name")
    if not isinstance(name, str) or not name or name != name.lower():
        return f"name 必须为小写 kebab-case: {name!r}"
    if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name) or name.startswith("-"):
        return f"name 含非法字符（只允许 [a-z0-9-]）: {name!r}"
    if not isinstance(m.get("version"), str) or not m["version"]:
        return "version 必须为非空字符串"
    if not isinstance(m.get("intent"), str) or not m["intent"]:
        return "intent 必须为非空字符串"
    steps = m.get("steps")
    if not isinstance(steps, list) or not steps:
        return "steps 必须为非空数组"
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict) or not isinstance(s.get("run"), str) or not s["run"]:
            return f"steps[{i}] 必须含非空字符串 run"
    verify = m.get("verify")
    if not isinstance(verify, list) or not verify:
        return "verify 必须为非空数组"
    for i, v in enumerate(verify, 1):
        if not isinstance(v, str) or not v:
            return f"verify[{i}] 必须为非空字符串"
    return None


def scan_package(path: str) -> tuple[dict | None, str]:
    """(entry, "") 或 (None, 错误信息)。files 表按 `mpkg.py check` 口径实算（含全部 zip 条目）。"""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if MANIFEST not in names:
            return None, f"zip 内无 {MANIFEST}"
        files = {rel: hashlib.sha256(z.read(rel)).hexdigest() for rel in names}
        m = json.loads(z.read(MANIFEST).decode("utf-8"))
    err = validate(m)
    if err:
        return None, err
    pid = content_id({"manifest": m, "files": files})
    fname = os.path.basename(path)
    want = f"{m['name']}-{m['version']}-{pid[7:19]}.mpkg"
    if fname != want:
        return None, f"文件名与实算 id 不符: 盘上 {fname}，应为 {want}"
    return {
        "name": m["name"],
        "version": m["version"],
        "id": pid,
        "file": f"packages/{fname}",
        "intent": m.get("intent", ""),
        "tags": m.get("tags", []),
        "author": m.get("author", ""),
        "size": os.path.getsize(path),
    }, ""


def count_attestations(registry: str) -> tuple[dict, list, list]:
    """扫 attestations/<name>/<id12>/*.json → (每 id 计数, 已解析回执, 解析失败)。"""
    root = os.path.join(registry, "attestations")
    counts: dict[str, int] = {}
    parsed: list[tuple[str, str]] = []
    bad: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in sorted(filenames):
            if not fn.endswith(".json"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, encoding="utf-8") as f:
                    a = json.load(f)
            except (OSError, ValueError) as e:
                bad.append(f"{full}: {e}")
                continue
            # 回执字段历史上有两种写法：id（现用）与 mpkg_id
            pid = a.get("id") or a.get("mpkg_id") or ""
            if not pid:
                bad.append(f"{full}: 无 id / mpkg_id 字段")
                continue
            counts[pid] = counts.get(pid, 0) + 1
            parsed.append((pid, full))
    return counts, parsed, bad


def build(registry: str, keep_dates: dict, keep_ver_dates: dict) -> tuple[dict, list[str], list[tuple[str, str]]]:
    """确定性重建 index。返回 (index 对象, 观察报告, 拒收清单)。

    空 index 一律拒绝生成（宁缺毋滥）。拒收（文件与自己的名字/身份对不上）由 main()
    决定是否放行 —— 拒绝**不**等于下架：悄悄丢一个坏文件会把「损坏」洗成「撤回」。
    """
    notes: list[str] = []
    rejects: list[tuple[str, str]] = []
    pkgdir = os.path.join(registry, "packages")
    names = sorted(f for f in os.listdir(pkgdir) if f.endswith(".mpkg")) if os.path.isdir(pkgdir) else []
    if not names:
        return {}, [f"packages/ 下没有 .mpkg（目录不存在或为空）: {pkgdir}"], []

    entries = []
    for fn in names:
        entry, err = scan_package(os.path.join(pkgdir, fn))
        if err:
            notes.append(f"REJECT {fn}: {err}")
            rejects.append((fn, err))
            continue
        # published_at = 首次登记时间。优先按文件路径保留；文件被重命名/重建（路径变了但
        # name@version 只有一个候选）时退化为按 name@version 继承 —— 不然修一个坏文件名
        # 就等于丢掉发布历史。有歧义（同版本多构建）时不继承，宁可留空也不猜。
        entry["published_at"] = keep_dates.get(entry["file"]) or keep_ver_dates.get(
            (entry["name"], entry["version"]), ""
        )
        entries.append(entry)
    if not entries:
        return {}, notes + ["没有任何合法包 → 拒绝生成空 index"], rejects


    counts, parsed, bad = count_attestations(registry)
    for e in entries:
        e["attestations"] = counts.get(e["id"], 0)

    known = {e["id"] for e in entries}
    for pid, full in parsed:
        if pid not in known:
            rel = os.path.relpath(full, registry).replace(os.sep, "/")
            notes.append(f"ORPHAN 回执指向未上架 id（多为此版本的旧构建）: {pid[:27]} {rel}")
    notes.extend(f"BAD 回执: {b}" for b in bad)

    # 确定性顺序：先 (name, version, id) 升序，再按 published_at 降序稳定排序（"" 落到最后）
    entries.sort(key=lambda e: (e["name"], e["version"], e["id"]))
    entries.sort(key=lambda e: e["published_at"], reverse=True)

    # 同 name@version 多 id = 该版本被重建过（publish 语义为「同版本替换」）；如实列出并提醒
    seen: dict[tuple, int] = {}
    for e in entries:
        k = (e["name"], e["version"])
        seen[k] = seen.get(k, 0) + 1
    for (n, v), c in sorted(seen.items()):
        if c > 1:
            notes.append(
                f"DUPLICATE {n}@{v}: 盘上有 {c} 个不同 id 的构建（同版本被重建过；install 按 published_at 取新）"
            )

    return {"registry": "mpkg", "version": "0.1", "packages": entries}, notes, rejects


def render(idx: dict) -> str:
    """与 mpkg.py 写入口径一致：ensure_ascii=False + indent=2 + 末尾换行。"""
    return json.dumps(idx, ensure_ascii=False, indent=2) + "\n"


def summarize(old: dict, new: dict) -> list[str]:
    """人可读的差异摘要（供 --check / 重建后输出，不写盘）。"""
    o = {p["file"]: p for p in old.get("packages", []) if p.get("file")}
    n = {p["file"]: p for p in new.get("packages", [])}
    out = []
    for f in sorted(set(n) - set(o)):
        e = n[f]
        out.append(f"  ADD    {e['file']}  {e['id'][:19]}  attest={e['attestations']}")
    for f in sorted(set(o) - set(n)):
        out.append(f"  DELETE {f}")
    for f in sorted(set(o) & set(n)):
        a, b = o[f], n[f]
        for k in ("id", "attestations", "published_at", "size"):
            if a.get(k) != b.get(k):
                out.append(f"  CHANGE {f} {k}: {a.get(k)!r} -> {b.get(k)!r}")
    if not out:
        out.append("  （包集合与字段全部一致，仅顺序或空白可能不同）")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(prog="reindex", description="mpkg 注册表确定性重建 / 校验")
    ap.add_argument("--registry", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--check", action="store_true", help="只校验不写盘（不一致退出码 2）")
    ap.add_argument(
        "--allow-reject",
        action="store_true",
        help="放行「文件名与实算 id 不符」的文件（把它们排除出 index）；默认 fail-closed 拒绝",
    )
    a = ap.parse_args()

    errs = self_test()
    if errs:
        print("契约源自检失败 —— 拒绝运行（先对齐 lystack/proto/mpkg/GOLDEN.md）", file=sys.stderr)
        for e in errs:
            print("  " + e, file=sys.stderr)
        return 3

    idx_path = os.path.join(a.registry, "index.json")
    old = {}
    if os.path.isfile(idx_path):
        with open(idx_path, encoding="utf-8") as f:
            old = json.load(f)
    keep_dates = {p["file"]: p.get("published_at", "") for p in old.get("packages", []) if p.get("file")}
    # name@version → 日期，仅在**唯一**时用作文件名变化后的回退（见 build()）
    ver_seen: dict[tuple, list] = {}
    for p in old.get("packages", []):
        k = (p.get("name"), p.get("version"))
        ver_seen.setdefault(k, []).append(p.get("published_at", ""))
    keep_ver_dates = {k: v[0] for k, v in ver_seen.items() if len(v) == 1}

    try:
        new, notes, rejects = build(a.registry, keep_dates, keep_ver_dates)
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        print(f"扫描失败: {e}", file=sys.stderr)
        return 1
    for n in notes:
        print("  " + n)
    if not new:
        print("拒绝生成空 index", file=sys.stderr)
        return 1

    # 拒收 = 盘上有文件与自己的名字/身份对不上（例如文件名说 A、内容实算 B）。
    # 这**不是**下架：把它当真会悄悄把「损坏」洗成「撤回」。所以 fail-closed：
    # check 模式判红（退出 2），写盘模式拒绝写（退出 4），除非显式 --allow-reject。
    if rejects and not a.allow_reject:
        print(
            f"[拒绝] {len(rejects)} 个包文件与自己的名字/身份对不上 —— 不写盘、不判过。",
            file=sys.stderr,
        )
        for fn, err in rejects:
            print(f"  {fn}: {err}", file=sys.stderr)
        print(
            "  真修法：把文件重命名为实算 id 的文件名（内容寻址下 id 是派生的，不是作者写的），"
            "或确认要下架就删掉文件。\n"
            "  确知要忽略这批文件才加 --allow-reject（它们会被排除出 index）。",
            file=sys.stderr,
        )
        return 2 if a.check else 4

    text = render(new)

    if a.check:
        with open(idx_path, encoding="utf-8") as f:
            raw = f.read()
        # 归一化 CRLF：Windows 工作副本（core.autocrlf）会写成 CRLF，但仓库 blob 是 LF。
        # 门禁只关心 JSON 内容与顺序，不关心平台换行（index.json 的字节不影响任何消费方：
        # mpkg.py 走 json.load，前端走 JSON.parse）。
        if raw.replace("\r\n", "\n") == text:
            print(f"[check] index.json 与实算一致（{len(new['packages'])} 包）")
            return 0
        print("[check] index.json 与实算不一致 —— 实算才是真相:", file=sys.stderr)
        for line in summarize(old, new):
            print(line, file=sys.stderr)
        print("修复: python tools/reindex.py （review 后提交）", file=sys.stderr)
        return 2

    with open(idx_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    total = sum(p["attestations"] for p in new["packages"])
    print(f"[reindex] {len(new['packages'])} 包 / {total} 回执 → {idx_path}")
    for line in summarize(old, new):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())


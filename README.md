# mpkg-registry —— 记忆包注册表

> **学习一次，所有 agent 复用。** 内容寻址、可回放验证、信任随回放复利。
> GitHub repo 即 registry（零基建，可镜像、可 U 盘传、可离线 fork）。

## 1. 目录 = 真相，index.json = 派生物

| 路径 | 角色 | 谁写 |
|---|---|---|
| `packages/*.mpkg` | **包文件本体**。identity = content-id（`sha256:` 前缀）；文件名 `<name>-<version>-<id12>.mpkg` 必须与实算 id 一致 | `mpkg.py publish` |
| `attestations/<name>/<id12>/*.json` | **独立回放回执**（每台机器一次真实回放一份）。信任的唯一来源 | `mpkg.py attest` |
| `index.json` | **派生索引**。= f(packages/, attestations/) —— 计数实算、身份实算、顺序确定 | `tools/reindex.py` |
| `index.html` | 展示/检索前端（运行时 fetch `index.json`，无自有状态） | 人工 |

**铁律：`index.json` 不得手改。** 它曾经是「先传包文件 → 再读改写 index」的两步写入 +
冗余计数存储，于是漂了（见 §5 审计）。现在任何 index 变更都必须来自重建器，
CI 会逐字符比对，手改必红。

## 2. 日常操作

```bash
# 发布一个包（打包 + 上架 + 写 index）
python3 mpkg.py publish <pkg-dir> -r lilyco-42/mpkg-registry

# 在**另一台机器**回放并上架回执（信任复利）
python3 mpkg.py verify <file.mpkg> --out att.json
python3 mpkg.py attest att.json -r lilyco-42/mpkg-registry

# 重建索引（publish/attest 之后，若 CI 报 index 漂移）
python3 tools/reindex.py

# 校验（CI 跑的就是这条；不一致退出码 2，契约源自检失败退出码 3）
python3 tools/reindex.py --check

# 安装（含 content-id 门禁 + 可选本机完整回放）
python3 mpkg.py install <name> -r lilyco-42/mpkg-registry --verify
```

### 下架协议（唯二能进真相层的两个动作）

注册表里没有「撤回标志位」——**下架 = 删文件**：

1. 删 `packages/<file>.mpkg`
2. 删 `attestations/<name>/<id12>/`（回执跟着包走，否则成为孤儿）
3. `python3 tools/reindex.py` 重建 → 提交

删一个包不影响任何已下载它的机器（内容寻址天然可离线），只影响市场可见性。

## 3. 契约来自哪里

mpkg 格式的**单一契约源**是 [`lyco-42/lystack` → `proto/mpkg/`](https://github.com/lilyco-42/lystack/tree/main/proto/mpkg)：
`mpkg.schema.json`（JSON Schema）、`golden/cases.json`（golden 向量）、`GOLDEN.md`（规则 + 实现漂移表）。

`tools/reindex.py` 内建 GOLDEN 三例自检（canon 幂等 + content-id 逐字节），**自检不过拒绝写盘**——
所以格式规则若变，这里会先失败，而不是悄悄算出另一个 id 把市场写歪。

## 4. 门禁

`.github/workflows/integrity.yml` 在每次 push/PR 跑 `tools/reindex.py --check`。
两条独立断言：契约源自检（GOLDEN 逐字节）+ index 与盘上实算一致。
「忘了重建 index」和「手改 index」都会红。


## 5. 审计记录（2026-09-27，血训与修复）

首次用 `tools/reindex.py --check` 对着盘上实测（而不是读文档），发现三处漂移：

| # | 现象 | 根因 | 处置 |
|---|---|---|---|
| 1 | `asr-code-rust-book@0.1.0` 记 `attestations: 1`，盘上真实回执 **2** 份 | 计数是**冗余存储**（回执在 attestations/，计数在 index），只靠 `attest` 时的 `+1` 增量维护，漏写即永久偏差 | 计数改为实算；CI 门禁钉死 |
| 2 | `kbv-video-distill-0.1.0` 盘上有 2 个不同 id 的构建，index 只列 1 个 | 同一 `name@version` 被 publish 两次（`cmd_publish` 语义是「同版本替换」），旧文件留在盘上、索引条目被覆盖 | 市场 = **目录内容**：两个构建都列出（内容不同 = 身份不同），重复由 `DUPLICATE` 提示显式暴露 |
| 3 | 3 个条目缺 `attestations` 字段 | 早期 `publish` / CI 写入口径不一 | 统一输出，字段形状固定 |

**修复后实测**（同一把尺量）：
- 7 个包文件的 content-id **全部**与 index 登记一致（防篡改锚完好）
- 6 份回执全部对上包 id，孤儿 0
- `--check` 退出码 0

**诚实边界（未做 / 待定）：**
- 回执里 `ok: false`（回放**失败**）目前不区分，只数文件个数。「失败次数」口径未定义
  （同一台机器反复失败算 1 次还是 N 次？），定义清楚前不假装有。
- 新发现的包文件 `published_at` 留空（真实发布时间已丢失，**不猜**）；首次登记后固定，
  此后重建不再改动。
- 本仓库不自动跑包内回放（执行外部代码不进 CI）；「信任复利」仍靠人工在多台主机上
  `verify` + `attest`。

## 6. 与 P0 链的关系

```
lilyco (<cli> --schema) → lyco_agent (从视频/手册产出 steps+atoms) → mpkg (打包)
   → cache-node verify (独立回放产出 attestation) → mpkg-registry (市场：本仓库)
```

本仓库是这条链的**信任落点**：上游任何一环说「这个包能跑」，最终都体现为这里的一份
回执文件 + 一个实算出来的计数。所以这里的唯一义务是**不撒谎**——
宁可显示 `attest 0`，也不显示一个没有回执支撑的数字。

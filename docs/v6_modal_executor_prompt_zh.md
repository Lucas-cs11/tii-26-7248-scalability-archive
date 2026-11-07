# 可直接发给新会话的执行提示词

你将接手一个 IEEE TII 论文《Bounded Task-Completion Diagnosability of Time Petri Nets: Verification and Firing-Interval Adjustment》的 Scalability Benchmark v6。请在当前机器的仓库目录执行，不要要求原用户再找实验 Agent，不要通过浏览器操作 GitHub。先完整阅读以下交接文件，它是本任务的事实基线和验收标准：

```text
/Users/lucas/Documents/Codex/2026-11-05/referenced-chatgpt-conversation-this-is-an/work/tii-scalability-v4/docs/v6_modal_executor_handoff_zh.md
```

仓库：

```text
/Users/lucas/Documents/Codex/2026-11-05/referenced-chatgpt-conversation-this-is-an/work/tii-scalability-v4
```

从分支 `scalability-v6-semantics-corrected` 的当前 HEAD 开始。先运行：

```bash
cd /Users/lucas/Documents/Codex/2026-11-05/referenced-chatgpt-conversation-this-is-an/work/tii-scalability-v4
git status --short --branch
git fetch origin
git rev-parse HEAD
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

任务目标不是尽快跑出 8,280 条结果，也不是降低 INC 或制造更好看的曲线。目标是形成可复核、强时间语义正确、完整保留不利证据的 v6 正式实验与最终 ZIP：

```text
TII_Scalability_Benchmark_v6_Corrected_FullScale_Results.zip
```

当前事实必须遵守：

1. P0 strong-time SCG/DBM 语义已修正，Fraction 独立 Oracle、A--F 反例回归和 12 家族语义检查已通过。不得改变 strong-time 或 single-server 语义来换取通过率。
2. 288 项冻结 Pilot 已在 GitHub Actions run `42323424751` 完成，并已下载到 `work/v6_pilot_run_42323424751/`。它得到 128 COMPLETED、160 TIMEOUT_WALL_CLOCK、8 INC、58 VER、62 AMB。原始 Pilot 不得重跑、覆盖或改写。
3. Pilot 的当前预算门禁为 false，正式 8,280 任务尚未冻结或启动。直接在 Modal 运行全量正式矩阵是不允许的。
4. Pilot 发现可修复的记录层问题：126 条 `SCG_COMPLETE` timeout 对未执行后续 counters 写了零。原始 artifacts 必须保留；审计 view 视其为 null。`runner/run_cell.py` 已为未来运行修复这一点。
5. 现在允许的技术工作是：实现可差分证明正确的同模型 SCG cache 和同模型任务同置，建立新的冻结 cache-validation protocol，测量真实节约，再重新评估预算门禁。不得做未经证明的 incremental/截断 SCG 或跨 L/H/mode 复用同步器。

对 Modal 的约束：

- 用户的 Modal token 曾在截图中暴露。不要读取、复制、打印、提交或使用截图中的 token。建议用户先在 Modal 控制台撤销/重签。
- 只有在实际执行环境中通过安全的只读检查确认 profile、账单余额/价格和 workspace budget cap 后，才可启动付费作业。没有这些信息时只做本地单元测试和 GitHub 小型 CI。
- 先用 `uvx modal profile list`、`uvx modal billing summary --for 'this month' --json`、`uvx modal billing rates --json` 做只读检查。输出中如出现敏感信息必须打码，不能写入仓库。
- 使用有上限的 CPU Function/Batch 任务、明确 timeout、有限并发、checkpoint 和持久 raw output；禁止无限重试、无限并发、long-lived sandbox 或未经授权的付费资源。

执行顺序：

1. 核验交接中列出的版本、Pilot artifacts、审计结果和单元测试。
2. 设计并实现 cache：以模型哈希、源码/SCG 实现哈希、语义版本、Python 版本和序列化版本组成严格 key；记录 hit/miss/build/load 时间和 provenance；cache 失败安全降级为 miss；默认 verifier 行为不变。
3. 添加差分测试，冷构造、内存预构造、序列化加载构造必须在所有语义结果、证书、SCG 标志、计数上相同，允许仅 runtime/cache metadata 不同。
4. 生成、提交、冻结一个独立 cache-validation 任务清单。它必须覆盖 12 家族、WIP 1/2/3、多 L、first/complete，以及 cache hit/miss；选择规则只能基于预注册结构，不能看 Pilot verdict 或 runtime。
5. 在 GitHub 或受限 Modal 运行 cache-validation，下载/保留全部 raw shard/attempt，独立审计，计算每家族和每实验 P50/P90/P95、右删失 timeout/INC、RSS、缓存差分和预算。不要为这一目的重跑已经确认的 Pilot timeout。
6. 只有当所有既有语义门禁、cache differential、内存/预算门禁以及 GitHub 或 Modal 资源政策门禁均通过时，才能生成并提交 `protocol/scalability_v6_frozen_protocol.yaml` 和 8,280 条不可变任务清单。固定配额为 structural 600、WIP 1,080、L-sweep 5,400、difficult 1,200。
7. 正式 run 必须产生 8,280 个唯一最终记录，严格区分 VER/AMB/INC/TIMEOUT_WALL_CLOCK/ERROR。只能重试基础设施故障，不重试已成功任务，不将 timeout 改成 INC 或 verdict。
8. 完成正式独立审计、36,450 历史结果影响审计、Figure 6、Section V-C/S7-B 建议、SHA256 manifest 和 ZIP 自检后交付。无正式通过时只能交付透明限制报告，不得称为可入稿。

所有代码/协议修改要提交并推送；所有 GitHub 操作只用 `gh` CLI/API。每个阶段只在有实质进展、失败、资源政策限制或需要用户作出付费决定时通知用户。不要编造后台运行、Modal 余额或 run ID。

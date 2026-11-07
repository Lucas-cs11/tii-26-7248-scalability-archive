# IEEE TII Scalability Benchmark v6：Modal 续执行交接包

## 交接结论

此仓库的 v6 工作已经完成 P0 语义修复、独立精确 Oracle 验证、12 个模型家族语义检查，以及冻结的 288 项 GitHub Actions Pilot。正式 8,280 项实验**尚未冻结、尚未启动**。原因是 Pilot 的 55.6% 任务在 60 秒外部时限下被右删失，按当前逐单元执行方案估算，正式矩阵最坏需要 8,280 runner-minutes，即 138 小时，还没有计入初始化、上传和技术重试。因此当前状态不是实验失败，而是正式预算门禁未通过。

接手者的任务是：在不修改数学语义、不回写或覆盖 v5 原始结果、不挑选有利数据的前提下，完成可证明正确的同模型 SCG 缓存与模型任务同置，进行小规模差分性能验证；只有新的预算门禁有量化证据支持，才能冻结 v6 正式协议并在 Modal 执行完整 8,280 项。若缓存后仍无法使预算和完成率达到可解释、可复核的标准，必须停止在正式重跑之前，提交量化阻断报告，而不是调参删样本或把 INC/TIMEOUT 当作结论。

## 工作目录与版本身份

仓库目录：

```text
/Users/lucas/Documents/Codex/2026-11-05/referenced-chatgpt-conversation-this-is-an/work/tii-scalability-v4
```

当前分支：`scalability-v6-semantics-corrected`。

当前已推送提交：

| 用途 | 提交 |
|---|---|
| v5 交付冻结基线 | `97b5d6db32279797580560ffcb82eccfdafd9b32` |
| v5 正式 GitHub 实验源码 | `dc7cae34310edacd6fdf3df9c2c4382ad715bd77` |
| v6 语义修复 | `f9bac23` |
| v6 独立 Oracle 与家族语义验证 | `0c6e6a1` |
| v6 语义 CI 锁定 | `b1ac5a0` |
| v6 Pilot 冻结 | `7b9ff58` |
| Pilot 独立审计、门禁拒绝和后续仪器修正 | `1415e61` |

开始前必须执行并保存输出：

```bash
cd /Users/lucas/Documents/Codex/2026-11-05/referenced-chatgpt-conversation-this-is-an/work/tii-scalability-v4
git status --short --branch
git fetch origin
git rev-parse HEAD
git log --oneline -8
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

任何新实验记录都必须包含实际 `commit_sha`、模型哈希、配置哈希、运行环境、执行器类型、墙钟时长、峰值 RSS、终止阶段和唯一 run/task 标识。

## 不可破坏的历史材料

以下内容只读保留，不能覆盖、删除、改写或作为 v6 结果重用：

- v5 的 120 个原始分片、8,280 条正式记录、任务清单和模型清单。
- v5 最终交付包：`outputs/TII_Scalability_Benchmark_v5_FullScale_Results.zip`。
- v6 Pilot 的 GitHub run `42323424751` 下载物：`work/v6_pilot_run_42323424751/`。
- Pilot 的独立审计输出：`work/v6_pilot_audit_42323424751/`。
- 两个 v5 源码快照：`work/v6_freeze/`。

新增数据一律写入新的、版本化目录，例如 `work/v6_cache_validation_<runid>/`、`work/v6_formal_<runid>/`。最终交付再复制到独立 staging 目录，不能用压缩过程修改原始材料。

## 已修复且已验证的强时间语义

旧 SCG 后继构造仅表示候选 firing date，却没有在投影前加入“被选择转迁不得晚于所有同时 enabled 竞争转迁”的约束，因此会接受 `[2,3]` 的正常转迁在 `[0,1]` 紧迫故障之前触发的伪路径。修复位于 `src/scg/builder.py::_compute_successor()`：在候选 zone 上加入每个竞争者的 `theta_t - theta_u <= 0`，closure 后若为空则拒绝后继，并以这个受限 DBM 计算 persistent/newly-enabled clocks。

不能通过改变论文定义的 strong-time 或 single-server 语义绕过此修复。必须持续保持：

- `[2,3]` 与 `[0,1]` 的紧迫竞争拒绝正常后继。
- `[2,3]` 与 `[1,3]` 的合法竞争保留两条可行分支。
- persistent transition 继承时钟，newly enabled transition 重置时钟。
- self-reenabling、DBM projection/closure/normalization、first-entry completion 都保持正确。

永久回归测试在 `tests/test_strong_time_scg.py`，覆盖 A--F 最小反例。`src/semantics/fraction_oracle.py` 是不调用 SCG 构造器或其 DBM 的 Fraction 精确有理数差分约束 Oracle；`tests/test_fraction_oracle.py` 有 15 个独立语言比较。语义 GitHub run `42322708113` 在提交 `b1ac5a024e79ac83b292ffc13005744a6511d0c9` 上通过 32 个单元测试和 12 个家族检查。证据见 `protocol/v6_semantics_gate.json`。

生成器已改为“即时路线选择，再在分支内执行定时动作”：检测使用无可观察标签的 `t_choose_skip`/`t_choose_inspect`，返工使用 `t_choose_continue`/`t_choose_rework`。这避免即时故障和延迟正常动作错误竞争。`scripts/validate_v6_model_semantics.py` 为 12 个家族构造独立的 timed witness，并检查资源不变量、正常/故障/检测/返工/完成路径和 WIP。新增即时选择不增加可观察词长度；这一点仍须在所有新协议中保持并记录。

## Pilot：冻结协议、真实结果与已知仪器问题

Pilot 构造文件：

- `scripts/build_v6_pilot.py`
- `protocol/v6_pilot_manifest.json`
- `protocol/v6_pilot_tasks/shard_000.csv` 至 `shard_023.csv`
- `scripts/validate_v6_pilot_protocol.py`
- `.github/workflows/scalability_v6_pilot.yml`

冻结前选择规则为 12 家族 × 2 个确定性种子 × 12 个预注册 strata；不会使用 verdict 选取。共有 288 个唯一配置、232 个模型哈希、280 个 distinct bounded problems。分组为 structural/observation 120、WIP 72、L-sweep 72、difficult 24。它是 Pilot，不是正式数据。

GitHub Actions run `42323424751` 在提交 `7b9ff58ef768c9cf4d642c095f36cb9fb23fa829` 上完成，25/25 jobs 成功。使用标准 `ubuntu-22.04`，24 分片，每片 12 任务，`max-parallel: 6`，每 cell 60 秒、每 job 30 分钟。所有 25 个 Actions artifact 已下载到本地，不应重复下载，除非先验完整性检查发现缺失。

独立审计脚本：

```bash
python3 scripts/audit_v6_pilot_results.py \
  --manifest protocol/v6_pilot_manifest.json \
  --tasks protocol/v6_pilot_tasks \
  --artifacts work/v6_pilot_run_42323424751 \
  --run-id 42323424751 \
  --source-sha 7b9ff58ef768c9cf4d642c095f36cb9fb23fa829 \
  --out work/v6_pilot_audit_42323424751
```

审计结果：288/288 唯一最终记录，无 ERROR、无 shard 缺失、无配置/模型哈希不一致，Peak RSS 全覆盖。记录计数为 128 `COMPLETED`、160 `TIMEOUT_WALL_CLOCK`、8 `INC`、58 `VER`、62 `AMB`；`INC` 不是确定诊断结论，`TIMEOUT_WALL_CLOCK` 也没有 verdict。

| 组别 | 任务 | 完成 | Timeout | INC | P50 | P90 | P95 | RSS P95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Structural/observation | 120 | 63 | 57 | 1 | 16.766 s | 60.091 s | 60.098 s | 169.078 MiB |
| WIP | 72 | 23 | 49 | 5 | 60.035 s | 60.097 s | 60.102 s | 251.945 MiB |
| L sensitivity | 72 | 40 | 32 | 0 | 8.934 s | 60.093 s | 60.099 s | 167.680 MiB |
| Difficult | 24 | 2 | 22 | 2 | 60.085 s | 60.111 s | 60.113 s | 253.965 MiB |

细分现象：WIP=2 是 24/24 timeout；WIP=3 为 19 timeout + 5 个 SCG state-limit INC；difficult 为 22/24 timeout；L low/mid/high 分别是 6/24、12/24、14/24 timeout。32 个 timeout 在 SCG 构造中停止，126 个在 SCG 完成后进入 synchronizer 时停止，2 个在 partial history analysis 停止。最大 Peak RSS 为 269.410 MiB，原始 artifact 总量 1.63 MB；工作流总耗时 183.05 runner-minutes，cell wall-time 和为 174.46 分钟。

Pilot 原始 checkpoint 有一项记录层缺陷：126 条在 `SCG_COMPLETE` 停止的 timeout 把后续未执行阶段的默认零写进了 raw counters。它们必须原样保留，审计 view 将其转为 `null`，不可解释为零工作。`1415e61` 已在 `runner/run_cell.py` 修正未来运行的 checkpoint null 语义；也修正了 partial-history checkpoint 的累计时间。所有未来 run 必须采用修正版本。该 Pilot 审计接受等级为 `PASS_WITH_INSTRUMENTATION_LIMITATION`，但预算门禁仍为 false。

## 当前唯一允许的优化方向

可以实施的优化仅限于语义等价、可差分证明的相同模型 SCG 重用，以及将相同模型的 L/H/mode 任务放到同一执行单元。缓存可以避免同一 TPN 重复构造 SCG，尤其对 L-sweep 有意义；它不能修复已经在 synchronizer 阶段出现的大部分 timeout。

推荐实现方式：

1. 在 `src/verifier.py::verify()` 增加可选 `prebuilt_scg` 和明确的 cache provenance 参数。默认路径必须保持现有行为。若传入 prebuilt SCG，跳过 `build_scg(tpn)`，`runtime_scg` 应记录为 0 或单独的 load 时间，绝不能冒充构造耗时。
2. 在 `runner/run_cell.py` 用模型哈希、源码 commit SHA、`src/scg/builder.py` 内容哈希、生成器语义版本、Python major/minor 和序列化格式版本组成 cache key。只有 key 完全匹配时才读缓存。
3. 使用本地 shard 范围临时目录，在同一 Modal worker 内复用；写入采用临时文件后原子 replace，损坏或反序列化失败必须视为 cache miss，并记录原因。不要让非可信 pickle 跨项目或跨版本共享。
4. 每条结果增加 `scg_cache_status` (`miss`/`hit`/`disabled`)、`scg_cache_key`、`runtime_scg_build_s`、`runtime_scg_load_s` 和 `cache_provenance`。这些字段不能混入原始算法计时指标。
5. 更改分片规划，使相同 `model_hash` 的多个 L/H/mode 任务同置。不得在 Pilot 后基于结果重选模型；只可按冻结的、明确的结构规则重新布局任务。
6. 在小模型和代表性模型上运行 cold build、fresh cached、serialize/load cached 三组差分测试。除 `runtime_scg` 与 cache metadata 外，必须比较 SCG classes、edges、completion IDs、zero-time-cycle flag、state-limit flag、最终 verdict、证书、所有工作量计数和原始/规范化 counters。任何差异都阻止缓存上线。
7. 不要实现按 L 截断 SCG、近似缓存、语义不明的 incremental SCG 或把同步器结果跨不相同 L/H/mode 重用，除非先给出形式等价证明和额外独立差分测试。

为防止把低风险单元性能推广到全体，必须新建一个独立、预注册的 cache-validation protocol。该 protocol 至少包含来自 12 个家族的固定代表、WIP=1/2/3、多个 L、first/complete 两种模式，以及预计 cache-hit 与 cache-miss 两种任务。它的选择依据应是结构与模型身份，不得依据 Pilot verdict 或运行时长。该验证可先在 GitHub Actions 小规模运行，或在 Modal 上受严格预算上限运行；不允许重跑已经确认的 Pilot timeout，只为形成更好看的完成率。

## Modal 使用条件和预算纪律

Modal 可用于 v6 的新 cache-validation 和后续正式分片，但启动任何会计费 workload 前必须满足以下条件：

1. 在**实际执行该任务的同一环境**中确认 Modal profile/workspace 能正常连接，并以只读方式取得当月账单摘要和计价；不要在日志、代码、文档或聊天中打印 access token、配置文件或完整认证输出。
2. 用户的 token 曾在截图中暴露。不要使用、复制、记录或回显该 token。请用户在 Modal 控制台撤销并重新签发；只使用该环境已安全配置的凭据。
3. 在 Modal 控制台为 workspace 设置明确预算上限。没有余额和上限的可验证信息时，只允许做本地单元测试和 GitHub 的小型 CI，不启动 Modal 付费作业。
4. 按实际 worker 规格、并发数、预估 runtime、数据传输和至少 25% 预留计算最坏成本。公开价格会变动，必须在启动时读取官方当前价格，记录检索日期与假设。不得将“可登录”当作“余额足够”。
5. 使用 CPU Function/Batch 风格的可中断任务，固定最大并发和每 task timeout，采用 checkpoint。禁止 bigger runner、无限重试、无限并发、长驻 Sandbox 或未授权付费服务。
6. 把每个 shard 的 stdout/stderr、checkpoint、最终 JSONL、环境锁定文件和 SHA manifest 写到可持久化结果目录。Modal dashboard/artifact 不是唯一归档。

建议在确认认证后先运行只读命令，并清洗可能敏感的输出：

```bash
uvx modal profile list
uvx modal billing summary --for 'this month' --json
uvx modal billing rates --json
```

如果 profile 显示 unknown、RPC 连接失败、余额/限额不可确认，停止在付费执行前，报告问题。不要猜测余额，也不要把凭据发给另一会话。

## Modal 运行器的最低要求

新建显式版本化文件，例如 `modal/v6_benchmark.py` 和 `scripts/run_v6_modal_shard.py`。不要替换 GitHub Pilot workflow。运行器应：

- 使用当前提交创建一个不可变 source bundle，记录 bundle SHA、依赖锁文件 SHA 和 Python 版本。
- 对每个 frozen task 生成或验证 `expected_model_hash`、`expected_structure_hash`、配置哈希。
- 为每个任务设独立外部 wall-clock timeout，区分 `COMPLETED`、`TIMEOUT_WALL_CLOCK`、`INC`、`ERROR`。`INC` 只表示算法在可运行完成后不确定；timeout 不得伪装为 INC。
- 在 `SCG_COMPLETE`/`SYNCHRONIZER_COMPLETE`/partial history 的 checkpoint 中将未执行阶段 counters 写为 JSON `null`，绝不能写 0。
- 记录 SCG、synchronizer、expansion、LP 分段时长，SCG classes/edges、sync states、candidate words、retained histories、LP calls、total wall time、peak RSS、终止阶段、attempt 数和 retry 理由。
- 对 infrastructure ERROR 只重试相同 frozen task，并保留每次 attempt；不要重试已验证成功，也不要反复重试语义 INC/TIMEOUT。
- 只把任务级成功和 artifact 上传成功都完成的 shard 标记为 final。
- 限制每个 worker 的任务量，使最长 shard 不超过配置的 job timeout；以模型同置为优先，但不要为缓存而造成过大的单 shard 风险。

## 新预算门禁与正式协议冻结

缓存验证结束后，必须独立审计，不得由运行器自己的汇总替代。生成至少以下项目：

- task manifest 与实际 raw JSONL 的一一对应，唯一 final record、无缺失、无重复、无 unexpected task。
- 每条记录的 source SHA、model/config hash、执行环境和 cache provenance 校验。
- raw hash 和文件 SHA-256 manifest；分别列出 shard/attempt/artifact。
- 分家族、分实验、分 WIP、分 L 的 P50/P90/P95、timeout/INC fractions、RSS P95，timeout 使用右删失语义，不把 60 秒当作实际完成时间。
- cold 与 cached 的差分完全一致证据，以及缓存实际节约的构造时间、对总时长的影响。
- 对正式 8,280 的保守预算：根据同置后的观察结果估计，并报告最坏上限、P90/P95 情景、并发和数据存储。不可用未验证的小样本均值替代高风险 WIP/difficult 情景。

只有同时满足以下门禁时，才可创建并提交 `protocol/scalability_v6_frozen_protocol.yaml` 和全量 8,280 项不可变任务清单：

```text
SCG_STRONG_TIME_PASS
SCG_SOUNDNESS_AND_COMPLETENESS_PASS
DBM_NORMALIZATION_PASS
ALL_12_FAMILIES_SEMANTIC_PASS
PILOT_BUDGET_AND_MEMORY_PASS
GITHUB_RESOURCE_POLICY_PASS 或经用户明确授权的 Modal budget policy PASS
CACHE_DIFFERENTIAL_PASS（若正式方案使用缓存）
```

正式任务数必须是固定的：structural-size 600、WIP 1,080、L-sweep 5,400、difficult 1,200，合计 8,280。任务清单冻结后不得为提高完成率而修改时间区间、故障率、观察标签、种子、L/H 网格、模型筛选或困难案例。若正式运行出现新的 P0 正确性问题，暂停受影响任务、修复、重做受影响协议冻结；不要混用新旧语义结果。

## 正式执行、审计和交付

正式执行后必须有 8,280 个唯一最终记录；每个记录均应归为 `VER`、`AMB`、`INC`、`TIMEOUT_WALL_CLOCK` 或 `ERROR`，且分类定义明确。不得因为 WIP=2/3 多 INC/TIMEOUT 就删除它们，也不得因为某个 L 不增长就删除该家族。所有原始 run/attempt/shard 都保留。

需另行完成 36,450 个历史 Proposed bounded evaluations 的影响审计，明确区分 v6 generator 特有问题、共享 SCG 核心问题、不受影响子集、需重算集合及成本；不得因 v6 通过便宣称历史数据全部有效。MSCG-LP Reference 的独立性和正确性也需单列。

只有通过正式独立审计后，才能从 v6 合格数据生成 Figure 6 的 SVG/PDF/EPS/PNG。图不能把 INC/timeout 的未执行后续工作量画成 0，也不能为了视觉效果删去不利数据。最终 ZIP 名称为：

```text
TII_Scalability_Benchmark_v6_Corrected_FullScale_Results.zip
```

至少包括：v5→v6 diff、根因和修复证据、Fraction Oracle、SCG/DBM regressions、12 家族 witness、Pilot 和 cache-validation 原始数据、预算报告和冻结协议、8,280 正式 raw/shards/attempts、历史影响审计、Figure 6 与脚本、独立复算报告、Modal/GitHub run IDs、完整 SHA-256 manifest、Section V-C/S7-B 最小修改建议、reviewer mapping 和 IEEE TII 适用性判定。没有正式 PASS 时，交付应明确写出限制，不能标注为可入稿。

## 必须避免的错误

- 不要把 v5 或 Pilot 结果拷贝成 v6 正式结果。
- 不要为了更好看的 Figure 6 挑模型、删除平台段、忽略 WIP=2/3 或将 INC/timeout 计作 VER/AMB。
- 不要把未执行后续阶段的 counters 记为 0。
- 不要声称后台工作、余额、GitHub/Modal run 已存在，除非有可复核 run ID 和 artifact。
- GitHub 操作只用 `gh` CLI/API，不使用浏览器轮询；使用定时/事件机制，状态无变化时保持安静。
- 不要把用户审批过的 v17 原稿蓝色段落直接改掉；只提供最小的 Section V-C 和 Supplementary S7-B 修改建议。
- 不要把任何 token、密钥、Modal 配置内容写入 commit、artifact、日志、交接文档或聊天。

## 首个可执行检查清单

1. 确认 HEAD 是 `1415e61` 或其后代，工作树干净，完整 unit suite 通过。
2. 对 Pilot artifact、audit、manifest 运行 SHA/数量检查，不重复下载。
3. 实现 cache API、key/provenance、同模型任务同置和差分测试；先在本地运行测试，再提交推送。
4. 运行新的、独立冻结的 cache-validation，审计其正确性和性能。此阶段不改旧 Pilot，也不启动 8,280。
5. 以缓存验证数据计算严格预算。余额、workspace budget cap 与估算均确认后，若所有门禁为 PASS，冻结正式协议；否则写出阻断报告并停在正式运行之前。
6. 若正式协议冻结，使用完全冻结清单在 Modal 分片执行，持续 checkpoint，处理仅限基础设施的技术重试。
7. 完整独立审计、历史影响审计、绘图、文件 manifest 和最终 ZIP 自检后才交付。

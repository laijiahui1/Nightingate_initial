# Nightingale 72-Hour Build — 需求拆分文档

> 来源：[2026 72 Hour Build_ Nightingale Candidate Brief 2.md](./2026%2072%20Hour%20Build_%20Nightingale%20Candidate%20Brief%202.md)
> 更新日期：2026-08-26

---

## 一、一句话定位

构建一个**单病人共享的纵向 Care Note 诊所 Web 应用**：以"时间线 + 协作 + AI 抄录 + 可追溯来源"为核心，让临床人员 10 秒内看懂患者全貌，同时把患者贡献的信息、AI 生成的摘要、多角色笔记整合成一个**可信、可溯源、可行动**的记录。

评分权重决定了优先级排序：**Glanceability(6) > Collaboration+AI(5) > Provenance(4) > Security(3) > Communication(2) > Bonus(10)**

---

## 二、需求分层

| 层级 | 内容 | 性质 |
|---|---|---|
| **P0 硬约束** | RBAC 服务端强制、Glance P95≤300ms(warm)、PHI 脱敏在 LLM 之前、仅合成数据、TLS+静态加密 | 必须 |
| **P0 核心功能** | Care Note 页 / Glance Top Card / 纵向时间线 / 内联协作 / 版本历史+回滚 / AI Scribe 集成 / RBAC / Provenance+冲突处理 | 必须 |
| **P0 交付物** | Git 仓库+微测试+README+2-3页技术简报+ATTRIBUTION.txt+演示视频(3场景) | 必须 |
| **P1 加分项** | 自学习 Importance Logic、混合存储/数据衰减、Ambient 语音采集(患者+临床) | 加分 |

---

## 三、模块划分（需完成的模块清单）

### M1. 数据模型与持久层（所有功能的基石）

- **核心实体及其关联**：`Patient` ↔ `Entry/Note` ↔ `Comment/Thread` ↔ `Version` ↔ `Highlight` ↔ `Provenance` ↔ `AI_Scribed_Note`
- 每条的元数据：`author_role`(patient/staff/clinician/system)、`author_id`、`timestamp`、`type`(session/consult/instruction/admin)、`provenance_pointer`
- 3 种 AI 抄录类型：`ai_doctor_consult_summary` / `ai_nurse_consult_summary` / `ai_patient_session_summary`，`author_role=system`
- 审计日志（**metadata only**，不存内容，符合测试要求）
- 建议：**PostgreSQL**（原生支持 RLS，直接满足"服务端强制 RBAC"）；版本存储可用快照或 diff（架构选择）

### M2. Care Note 单页（每个患者一个统一页面）

- **Glance / Top Card**：可读+可行动，10 秒内消化；展示开放动作（如"需要化验单""等待护士跟进"）和风险/标记
- **纵向时间线**：按时间排序的连续信息流，混合手动笔记与 AI 抄录笔记
- **内联协作**：线程评论 + resolve/unresolve、@提及、任务指派（"Assign to staff"）
- **版本历史**：完整快照、"view changes since X"、回滚到任意版本
- **实时协作**：像 Google Docs 的多角色并发编辑（WebSocket）

### M3. Glance 高亮 / Smart Prioritization 引擎

- 核心逻辑：综合 **时效性 + 显式 risk_level + 临床实体(药物/主诉/过敏) + 未解决任务**
- 每个高亮必须展示 **risk_reason + provenance_pointer**，支持快速 接受/拒绝
- **自学习组件(加分)**：学习临床人员对 AI 笔记中某些内容的手动高亮/编辑/评论行为 → 调整未来建议优先级（权重需持久化）

### M4. RBAC 权限系统（服务端强制）

- 4 角色：patient / staff / clinician / admin
- 规则：患者不能看内部评论和原始 AI 笔记；staff 只能看+写 staff_notes 且不能跨诊所；clinician 可读写 clinician_sections、看 staff+AI 笔记、诊所范围；admin 诊所范围内全局可见
- 约束：**Clinician 不能覆盖 Staff 笔记，Staff 不能覆盖 Clinician 笔记**
- 实现：Postgres RLS + 中间件/后端检查双重兜底（UI 检查不算数）

### M5. Provenance & Trust 模块

- 点击任意高亮 → 跳转到时间线中的**源头条目/片段**
- 冲突处理：临床编辑优先于 AI/患者记忆，**或**标记冲突供复核（两条路径都要体现）

### M6. 安全与隐私管线

- **No PHI Redaction Pipeline**：姓名、IC/身份证号、电话 → 在发送 LLM 之前脱敏
- TLS 传输 + 静态加密
- **干净日志**：日志中不泄露 PHI
- 全部使用合成数据 + 合成数据种子生成器

### M7. Ambient Voice Capture（加分，两套独立视图）

- **患者端**（仅患者视图）：PWA 移动端录音 → LLM 前脱敏 → 转写 → 结构化事实提取 → 生成患者 consult 摘要
- **临床端**（仅临床视图）：PWA 移动/笔记本录音 → 带说话人标签的转写、时间戳、置信度标记、代码切换支持、临床摘要、来源段落追溯
- 进阶加分：嘈杂环境、说话人分离、重叠语音、多语言医学术语、多设备采集

### M8. 微测试套件（5 个指定文件）

- `test_rbac_scope.py`（角色互不可写+患者隔离）
- `test_revision_history.py`（版本递增+回滚+审计日志 metadata）
- `test_highlight_provenance.py`（每个高亮 provenance 可解析）
- `test_concurrent_edits.py`（不同区段并发不互相覆盖+同区段冲突的确定性解决）
- `test_self_learning_importance.py`（加分：模拟 pin 高亮→同类内容优先级提升）

### M9. 交付物文档

- README（setup/run、**脱敏在哪发生**、**RBAC 如何强制**）
- 2-3 页技术简报（架构图+完整数据 schema+假设/权衡）
- ATTRIBUTION.txt（所有外部库/模型及许可证）
- 演示视频覆盖 3 个场景（A: Glance+AI Scribe 追溯；B: 协作+审计+学习；C: 纵向历史+高亮逻辑+数据衰减）

---

## 四、潜在 / 隐含需求清单（容易漏掉）

1. **多诊所隔离（Multi-clinic tenancy）** — "access is clinic-scoped" 反复出现，schema 需带 clinic_id，RLS 按诊所过滤
2. **10 秒 glance 隐含预聚合** — Top Card 必须预计算/缓存，否则达不到 300ms 与"快速读取"双目标
3. **并发冲突的确定性策略** — 测试明确要求；同区段冲突需定义（如：时间戳/版本号/字段级合并 + 冲突标记）
4. **提及/任务指派 → 通知机制** — @nurse @clinician 语义上隐含通知/任务队列
5. **resolve/unresolve 状态机** — 评论生命周期需建模
6. **学习权重持久化** — 自学习机制必须落库（learning_weights / interaction_log）
7. **"view changes since X"** — 需要可时间点 diff 的能力，不只是回滚
8. **临床实体识别** — 高亮逻辑需识别药物/主诉/过敏等实体（可用 LLM 或 NER）
9. **LLM 信任心理学** — Brief 反复强调"trust but need reassurance"：UI 设计要体现人类可复核、来源可见，这是**评分隐性项**
10. **AI 笔记 vs 手动笔记的视觉区分** — author_role=system 需在时间线上清晰可辨
11. **语音采集的 PHI 脱敏时机** — 必须在转写→LLM 之前完成，且要覆盖所有数据流
12. **多设备/跨端一致性** — PWA 采集 + Web 端查看的数据同步
13. **演示可复现性** — 需要**合成数据种子脚本**让三个 demo 场景能一键演示
14. **日志合规** — "clean logs" 意味着脱敏管线也要作用于日志输出
15. **数据衰减策略**（加分）— 旧数据压缩/归档 schema + 逻辑，与时间线展示配合
16. **测试运行方式文档化** — "how to run tests" 是交付要求

---

## 五、技术选型建议（基于约束推断）

| 层 | 建议 | 理由 |
|---|---|---|
| 后端 | **Python / FastAPI** | 测试文件名全是 `.py`，强暗示测试为 Python；FastAPI 快速迭代 |
| 数据库 | **PostgreSQL** | 原生 RLS 直接落地"服务端强制 RBAC" |
| 前端 | **React / Next.js + Tailwind** | 快速构建协作 UI |
| 实时协作 | **WebSocket** | Google Docs 式并发 |
| 转写 | **Whisper** 系列 | 说话人标签/多语言/嘈杂环境支持 |
| LLM | 任选（OpenAI/Claude） | 摘要+高亮建议+实体提取 |

---

## 六、建议的落地顺序

1. 数据模型 + Postgres schema + RLS（一次性打好地基）
2. Glance/Top Card（最高分 6 分，优先）
3. 时间线 + AI Scribe 三类笔记接入
4. 协作（评论/提及/任务）+ 版本历史
5. RBAC + 脱敏管线 + 5 个测试文件（先写测试后实现更稳）
6. Provenance 跳转 + 冲突处理
7. 加分项：自学习、数据衰减、语音采集

---

## 七、验收标准映射（评分维度 → 模块）

| 评分维度 | 分值 | 对应模块 |
|---|---|---|
| Glanceability & Actionability | 6 | M2(Glance) + M3 |
| Collaboration & AI Integration | 5 | M2(协作/版本) + M1 + M3(AI 集成) |
| Provenance & Trust | 4 | M5 |
| Security & Privacy | 3 | M4 + M6 |
| Communication | 2 | M9(简报/演示) |
| Bonus: Nightingale Alignment | 10 | M3(自学习) + M7 + 数据衰减 |

---

## 八、硬约束核对清单（提交前逐项确认）

- [ ] RBAC 服务端强制（RLS + 后端检查），非 UI 层
- [ ] Glance P95 ≤ 300ms（warm path），并在简报中说明测量方法
- [ ] PHI（姓名/IC/身份证/电话）在发送 LLM **之前**脱敏
- [ ] 全部使用合成数据
- [ ] TLS 传输 + 静态加密
- [ ] 5 个测试文件齐全且可通过，README 说明运行方式
- [ ] README 说明脱敏在哪发生、RBAC 如何强制
- [ ] 2-3 页技术简报 + 架构图 + 完整数据 schema + 权衡
- [ ] ATTRIBUTION.txt（所有外部库/模型 + 许可证）
- [ ] 演示视频覆盖 A/B/C 三个场景

# 模拟对练质检/修复生成评测集与数据中心分类管理落地技术方案

> **方案版本**：v2.1（全栈落地闭环版）  
> **核心原则**：**“原貌提取、零花架子、原始序号防移位、规则驱动零硬编码、双源锚定真标答、微调对话双向同步”**。彻底剔除自然语言虚假正则抽取与冗余代码推演，以真实工程数据为唯一事实源，打通多轮飞轮数据向基准评测集转化的完整闭环。

---

## 一、 需求背景与核心挑战

在数据飞轮闭环中，多轮对弈经质检与修复后，产出的是完整 SFT 会话（供全参/LoRA微调）。为验证微调后新模型是否切实修复了这些问题，必须同时生成**基准评测集（Benchmark Dataset）**。但在落地过程中面临六大核心技术挑战：

1. **剪枝序号位移（Index Shift Drift）**：
   修复引擎执行 `prune`（成对剪枝）后，完整对话的物理长度被缩短。如果用数组物理下标直接截取上下文，会导致后序考题的上文严重错位，甚至把该题自身的回复也塞进上文；
2. **带病上文污染（Causal Contamination）**：
   一通长对话可能包含多个缺陷轮次。若为后序考题切题时使用原始对话，前序未纠正的违规话术将残留于上文中，对被测模型造成误导；
3. **真实标答双源锚定（Truth Anchoring）**：
   若仅以质检扣分项切题，当修复模型漏修某轮时，原本违规被扣分的话术会被荒谬地当作评测集“标准参考答案 (reference)”；切题必须严格以**成功执行 replace 纠正的坐席轮次**为事实锚点，反查挂载 rubric；未修复轮次坚决不切题；
4. **外呼首轮与角色交替防线（Outbound Turn-1 Defense）**：
   外呼场景第一轮往往是坐席开场白，若首轮被扣分导致 target_turn=1，前序历史集合为空，切出的 messages 只有 system 或为空，模型推理直接抛 400 Bad Request；切题必须具备自动呼通引导语补位（`[系统电话已接通，请开始外呼播报]`）与严格以 user 结尾收尾防线；
5. **微调与评测双向同步（Two-Way Synchronization）**：
   用户在测评切题抽屉中微调了 reference，必须原子化同步回写 `sample.remediated_messages_json`，杜绝 SFT 训练集与 Benchmark 评测集标准答案产生语义脱节；
6. **资产混同与数据倾斜（Decoupling & De-biasing）**：
   数据资产中心将多轮微调语料与基准评测集双 Tab 解耦；批量导出时提供“全量缺陷切题”与“每会话精选首错题”双策略，防止长对话多考题引发前缀数据倾斜。

---

## 二、 核心架构与数据流图（Architecture & Data Flow）

```mermaid
flowchart TD
    subgraph 对弈与质检修复
        A["多轮对话原始消息 (raw_messages)"] --> B["白盒质检 (UniversalJudgeEngine)\n检出缺陷 deductions (带原始轮次 orig_turn)"]
        B --> C["声明式微创修复 (UniversalDataRemediator)\n执行 prune 剪枝 + replace 替换\n维护不可变 items[orig_turn, deleted, content]"]
    end

    subgraph 核心切题引擎 (ProgressiveContextSlicer)
        C --> D["遍历 deductions 中的靶向轮次 (target_turn)"]
        D --> E["1. 过滤已剪枝轮次 (not deleted) 且 orig_turn < target_turn\n2. 继承前序已被 replace 纠正的黄金话术\n➔ 生成纯净考题 messages (以客户提问收尾)"]
        D --> F["按 orig_turn == target_turn 准确定位修复后的坐席回复\n➔ 登记为参考答案 reference (真实修复文本)"]
        D --> G["直接取扣分项 item + reason\n➔ 登记为评分细则 rubric"]
        D --> H["regex_constraints 默认置空 [] (不猜自然语言)"]
        E & F & G & H --> I["组装为 FlywheelBenchmarkSlice 评测切片"]
    end

    subgraph 人工复核与同步更新 (Workbench & DatasetCenter)
        I --> J["飞轮复核工作台:【测评切题与参考答案】面板\n可视化展示每道考题"]
        J --> K["人工按需微调 (修改 reference / rubric / regex)"]
        K --> L{"保存操作"}
        L -- "保存切片修改" --> M["持久化至 sample.benchmark_slices_json"]
        L -- "保存为评测集" --> N["写入 Benchmark 评测集文件 (.jsonl)\n同步注册至数据资产中心 (dataset_type='benchmark')"]
    end

    subgraph 数据资产中心双Tab解耦
        N --> O["🎯 常规评测集 Tab\n(一键发起对比评测 / 抽检题目)"]
        C --> P["📦 训练与对话数据 Tab\n(SFT微调多轮会话导出 / 抽检对话)"]
    end
```

---

## 三、 核心算法：不可变原始轮次过滤切题（Original Turn Indexing Slicer）

### 3.1 核心算法原理
为彻底解决 `prune` 剪枝导致的数组下标错位，切题引擎完全脱离物理数组下标，**全程依托原对话每个轮次不可变的原始序号（`orig_turn = 1, 2, ..., N`）进行状态过滤**。

### 3.2 算法执行步进（无 LaTeX，纯文本推导）
1. **状态项初始化**：
   系统在修复执行器（`apply_remediation_actions`）中，为原对话的每一轮分配唯一的 `orig_turn`（1 到 N），初始状态：
   `items = [{"orig_turn": i, "role": msg.role, "content": msg.content, "deleted": False}, ...]`
2. **应用修复处方单（Actions）**：
   - 若执行 `replace(turn=k, text=new_text)`：
     找到 `orig_turn == k` 的项，就地更新 `content = new_text`；
   - 若执行 `prune(turns=[p1, p2])`：
     找到对应项，标记 `deleted = True`（不从数组中物理剔除，保证其他项的序号永远不变）；
3. **针对缺陷轮次（target_turn）切题生成**：
   从质检扣分项中获取需要考核的坐席轮次序号 `target_turn`（如第 8 轮）：
   - **参考答案 (`reference`)**：
     通过 `orig_turn == target_turn` 直接定位到该项，取其修复后的文本：
     `reference = target_item.content`
     （无论前面删了多少轮，用不可变的 orig_turn 永远 100% 精准命中该轮修改后的真实答案）；
   - **考题上下文 (`messages`)**：
     收集所有满足：`orig_turn < target_turn` 且 `deleted == False` 的轮次：
     `context_turns = [ChatMessage(role=it.role, content=it.content) for it in items if it.orig_turn < target_turn and not it.deleted]`
     `messages = ([system_msg] if system_msg else []) + context_turns`
     **数学必然性**：
     - 被 prune 剪除的冗余轮次因为 `deleted == True` 被自动剔除，绝不污染上文；
     - 前序被 replace 修复的轮次，其 `content` 已经更新为黄金话术，自然流向下文；
     - 最后一轮严格停在目标坐席回复前的一条客户发言（user）；
     - 彻底消除位移越界与上下文穿帮！
   - **评分细则 (`rubric`)**：
     直接取质检扣分原生事实：`f"{deduction['item']}: {deduction['reason']}"`，绝不凭空注水；
   - **正则约束 (`regex_constraints`)**：
     默认置空 `[]`，只有规则显式配置了固定红线词时继承，绝不从自然语言中妄图抽取正则；
   - **同轮多扣分项合并（Co-occurring Deductions）**：
     若同一轮次同时被 FAQ 和流程红线两个质检官扣分，合并为一个复合考题，rubric 简单并列两项扣分原因，避免生成两道输入完全相同的冗余考题。

---

## 四、 规则中心去硬编码设计（Rule-Driven Policy）

代码层坚决剔除任何“必须三段式拉回主线”的硬编码假设：
1. **纯被动客服或无流程场景（`topology_sop.enabled == False`）**：
   - 规则中心配置说明：“解答客户问题后，礼貌询问是否还有其他问题需要协助”；
   - 质检法官只考核“政策是否答对、是否礼貌询问其他问题”；
   - 修复模型产出的答案就是“解答 + 询问还有其他问题吗”；
   - 切题引擎直接将该回复登记为 `reference`，不强加任何拉回主线动作；
2. **会话收尾或客户挂机场景**：
   - 系统内置 `detect_customer_hangup` 机制，识别客户忙碌/挂机意向；
   - 质检豁免流程推进，修复模型输出礼貌道别；
   - 考题的 `reference` 天然就是道别收口。

---

## 五、 数据结构契约与数据库设计

### 5.1 数据契约（`FlywheelBenchmarkSlice`）
位于 `backend/app/schemas/flywheel.py`：
```python
class FlywheelBenchmarkSlice(BaseModel):
    slice_id: str                          # 切片唯一ID (例如 slice_sample001_turn8)
    target_turn: int                       # 靶向原始轮次序号 (orig_turn)
    defect_item: str                       # 质检扣分项名称
    defect_reason: str                     # 质检扣分具体原因
    messages: List[ChatMessage]            # 状态过滤后的累积纯净上文 (以客户提问结尾)
    reference: str                         # 修复后的实际回复文本 (支持在线编辑)
    rubric: str                            # 评分细则 (格式: item: reason，支持在线编辑)
    regex_constraints: List[str] = Field(default_factory=list) # 正反向正则 (默认空列表，支持在线增删)
    category: Optional[str] = "飞轮实战质检" # 考题分类
```

### 5.2 数据库存储与平滑迁移
在 `backend/app/models/base.py` 的 `FlywheelSampleRecord` 中增加字段：
```python
benchmark_slices_json = Column(Text, default="[]")  # 持久化存储本会话切题及人工微调结果
```
在 `init_db()` 中增加轻量级幂等探测：
```python
# 检测 flywheel_samples 是否包含 benchmark_slices_json 列，若不存在执行 ALTER TABLE 添加，保证现有数据库平滑兼容
```

### 5.3 RESTful API 契约

| 接口 | 方法 | 功能描述 | 请求参数 / Body |
| :--- | :--- | :--- | :--- |
| `/api/v1/flywheel/samples/{id}/slices` | `GET` | 获取当前样本的评测切片（优先读库，未生成则即时切片） | `id` (样本ID) |
| `/api/v1/flywheel/samples/{id}/slices` | `PUT` | 保存人工在抽屉中微调后的切题数据（修改 reference/rubric/regex） | `UpdateBenchmarkSlicesRequest` |
| `/api/v1/flywheel/samples/{id}/export-benchmark` | `POST` | 将当前样本的评测切片保存/导出为标准 Benchmark 评测集 | `SaveBenchmarkDatasetRequest` |
| `/api/v1/flywheel/batch-export-benchmark` | `POST` | 批量将多个勾选样本的切片合并导出为 Benchmark 评测集 | `BatchExportBenchmarkRequest` |

---

## 六、 前端交互设计规范

### 6.1 数据资产中心（`DatasetCenter.tsx`）双 Tab 解耦重构
页面顶部使用 Ant Design `<Tabs defaultActiveKey="benchmark">` 彻底分离两类资产：
1. **Tab 1: 🎯 常规评测集 (Benchmark Datasets)**：
   - 过滤展示 `dataset_type === 'benchmark'`；
   - 核心功能：
     - **「⚡ 一键发起评测」**：点击后直接跳转评测任务发起抽屉，并自动选中当前评测集；
     - **「抽检预览」**：专属渲染评测题卡片（`messages` 上文气泡、`reference` 标准答案、`rubric` 及 `regex_constraints`）；
     - **「下载」**、**「删除」**；
   - 导入弹窗：默认类型固定为 `benchmark`。
2. **Tab 2: 📦 训练与对话数据 (Dialogue / SFT Datasets)**：
   - 过滤展示 `dataset_type !== 'benchmark'`（如 `flywheel_gold`、`training`）；
   - 核心功能：
     - **「抽检对话」**：专属渲染完整多轮会话（首轮 system + user/assistant 严格交替气泡）；
     - **「下载 SFT 语料」**、**「删除」**；
   - 导入弹窗：默认类型固定为 `flywheel_gold`。

### 6.2 飞轮复核抽屉【测评切题与参考答案】面板
在 `FlywheelWorkbench.tsx` 的单样本抽屉中，提供选项卡切换：
- **Tab 1: 🔀 多轮会话 Diff 与微调**（原有整篇对话比对视窗）；
- **Tab 2: 📝 测评切题与参考答案（新增）**：
  - 顶部操作条：`[💾 保存切片修改]`、`[🚀 保存为 Benchmark 评测集]`；
  - 逐题卡片平铺：
    - **标题栏**：`🎯 轮次 #Turn {target_turn} - {defect_item}`；
    - **评测输入上下文 (`messages`)**：折叠面板，展开可查看前序上下文（明确展示已被修复的前序话术，被 prune 轮次已不可见）；
    - **参考答案 (`reference`)**：多行文本输入框，直接展示修复后的坐席回复，支持直接改字；
    - **评分细则 (`rubric`)**：单行/多行文本输入框，直接展示原生质检扣分原因，支持直接编辑；
    - **正反向正则 (`regex_constraints`)**：Tag 列表，默认若为空提示“（未配置，仅依赖评分细则评估）”，支持点击 `+ 添加约束`（可添加如 `!违禁词`）。

### 6.3 批量导出为评测集
在 `FlywheelWorkbench.tsx` 表格顶部的批量浮动操作条中：
- 勾选多个复核样本后，点击 **「🚀 批量导出为评测集」**；
- 弹出 Modal：输入评测集 ID（如 `flywheel_batch_eval_v1`）、名称与模式（新建 / 追加）；
- 后端一次性收集所选样本的所有切片（优先采用人工已微调保存的切片），合并生成 `.jsonl` 并在 `datasets` 表中注册，数据资产中心即刻可见并可一键评测！

---

## 七、 落地实施步骤与验收路径

1. **Step 1：核心切题引擎与后端 API 实现**
   - 编写 `backend/app/engine/slicer.py` 实现 `ProgressiveContextSlicer`；
   - 扩展 `backend/app/schemas/flywheel.py` 与 `backend/app/models/base.py`；
   - 在 `backend/app/api/flywheel.py` 实现切片查询、修改持久化、单样本与批量导出接口；
   - 编写测试用例 `backend/tests/test_benchmark_slicer.py`，专门测试带 `prune` 剪枝下的序号对齐与切片准确性。
2. **Step 2：数据资产中心（DatasetCenter.tsx）双 Tab 重构**
   - 增加 Tabs 切换，将常规评测集与训练微调集解耦；
   - 优化导入弹窗与抽检预览展示。
3. **Step 3：飞轮复核抽屉【测评切题与参考答案】面板开发**
   - 在复核抽屉增加选项卡，实现切片卡片流渲染与在线编辑；
   - 对接保存修改与保存为评测集接口；
   - 表格批量操作栏增加批量导出评测集功能。
4. **Step 4：端到端回归验证与文档同步**
   - 运行全部测试套件确保 0 回归；
   - 同步更新 `AGENTS.md` 与 `lessons.md`。

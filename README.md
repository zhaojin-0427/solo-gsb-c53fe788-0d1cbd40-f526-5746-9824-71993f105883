# 有状态接口升级兼容性检查服务

基于 **Python + FastAPI + z3-solver** 的 HTTP API：给定同一接口的**旧版**与**新版**
状态模型，检查在**至多 8 次请求**内，旧版的每一条请求/响应轨迹是否都能由新版重现
（有界轨迹包含检查）。失败时返回**最短反例**（并按请求名、参数、状态码、标签的轨迹
字典序破同分），附旧版执行路径与新版逐步可达状态；成功时仅声明已检验的边界。

## 模型格式

`POST /compare` 的请求体：

```json
{
  "old": { "…状态模型…" },
  "new": { "…状态模型…" },
  "max_requests": 8
}
```

`max_requests` 可选，默认 8，取值 1–8。状态模型：

| 字段 | 说明 |
| --- | --- |
| `initial_state` | 初始控制状态（字符串） |
| `registers` | **至多两个** 32 位无符号整数寄存器，`{"x": 0, "y": 5}`，初值 ∈ [0, 2³²−1] |
| `transitions` | 迁移列表（见下） |

迁移（`transitions[]`）：

| 字段 | 说明 |
| --- | --- |
| `id` | 可选，缺省自动分配 `t0, t1, …` |
| `from` / `to` | 源/目标控制状态 |
| `request` | 可选。设置后为**请求迁移**：消费该具名请求，且必须带 `response`；不设置则为 **τ 步**（无输入输出，不得引用参数、不得带 `response`） |
| `guard` | 可选，线性比较的合取：`{"terms": [{"reg": "x", "coef": 1}, {"param": true, "coef": -1}], "op": "<=", "const": 0}` 表示 `x − param ≤ 0`。`op` ∈ `<, <=, ==, !=, >=, >` |
| `update` | 可选，按寄存器给出更新：`const`（赋常数）、`param`（取参数）、`add`/`sub`（加减常数） |
| `response` | 请求迁移必填：`{"status": 200, "label": "ok"}` |

语义规则：

- 每次请求前**至多两次 τ 步**（0、1 或 2 步），τ 步不可观测；
- 迁移可走的条件是守卫成立**且**所有寄存器更新结果仍在 [0, 2³²−1] 内——
  **越界更新不可走**；
- 请求参数是单个 32 位无符号整数（0…2³²−1），新旧两版观测同一条轨迹
  （同一请求名、同一参数、同一状态码、同一标签）；
- 同一请求的不同响应（不同状态码/标签）都必须能被新版逐一重现。

判定结果：

- `compatible: true` —— 仅声明已检验边界：至多 `checked_requests` 次请求内
  旧版轨迹均可由新版重现（有界结论，不外推）；
- `compatible: false` —— 返回**最短**反例轨迹；并列时按
  （请求名 → 参数 → 状态码 → 标签）的轨迹字典序取最小者，并给出：
  - `old_path`：旧版逐步执行路径（含 τ 步、每步后的寄存器值）；
  - `new_reachable`：新版在每个轨迹前缀后的可达配置集合（最后一项为空集，
    即新版在此处无法复现）；
- 模型非法（寄存器超过两个、初值越界、τ 引用参数、请求迁移缺 `response`、
  引用未定义寄存器等）→ **HTTP 422**。

## 启动

### Docker Compose（一键）

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 访问

- 交互式文档（可直接试调）：`http://localhost:8000/docs`
- 健康检查：`GET http://localhost:8000/health` → `{"status":"ok"}`
- 兼容性检查：`POST http://localhost:8000/compare`

## 请求/响应示例（实测）

### 1. 反例：新版漏掉了同一请求的另一种响应

```bash
curl -s -X POST http://localhost:8000/compare \
  -H 'Content-Type: application/json' \
  -d '{
    "old": {
      "initial_state": "idle",
      "registers": {"stock": 0},
      "transitions": [
        {"id": "t0", "from": "idle", "to": "idle", "request": "restock",
         "update": {"stock": {"kind": "param"}},
         "response": {"status": 200, "label": "restocked"}},
        {"id": "t1", "from": "idle", "to": "idle", "request": "buy",
         "guard": [{"terms": [{"reg": "stock"}], "op": ">=", "const": 1}],
         "update": {"stock": {"kind": "sub", "value": 1}},
         "response": {"status": 200, "label": "sold"}},
        {"id": "t2", "from": "idle", "to": "idle", "request": "buy",
         "guard": [{"terms": [{"reg": "stock"}], "op": "==", "const": 0}],
         "response": {"status": 409, "label": "sold_out"}}
      ]
    },
    "new": {
      "initial_state": "idle",
      "registers": {"stock": 0},
      "transitions": [
        {"id": "n0", "from": "idle", "to": "idle", "request": "restock",
         "update": {"stock": {"kind": "param"}},
         "response": {"status": 200, "label": "restocked"}},
        {"id": "n1", "from": "idle", "to": "idle", "request": "buy",
         "guard": [{"terms": [{"reg": "stock"}], "op": ">=", "const": 1}],
         "update": {"stock": {"kind": "sub", "value": 1}},
         "response": {"status": 200, "label": "sold"}}
      ]
    }
  }'
```

响应（HTTP 200）：

```json
{
  "compatible": false,
  "counterexample": {
    "length": 1,
    "trace": [
      {"request": "buy", "param": 0, "status": 409, "label": "sold_out"}
    ],
    "old_path": [
      {
        "kind": "request", "id": "t2", "from": "idle", "to": "idle",
        "request": "buy", "param": 0, "status": 409, "label": "sold_out",
        "registers": {"stock": 0}
      }
    ],
    "new_reachable": [
      [{"state": "idle", "registers": {"stock": 0}}],
      []
    ]
  }
}
```

### 2. 反例：τ 步初始化的寄存器不同，第二条轨迹才分叉

```bash
curl -s -X POST http://localhost:8000/compare \
  -H 'Content-Type: application/json' \
  -d '{
    "old": {
      "initial_state": "boot",
      "registers": {"credits": 0},
      "transitions": [
        {"id": "t0", "from": "boot", "to": "ready",
         "update": {"credits": {"kind": "const", "value": 2}}},
        {"id": "t1", "from": "ready", "to": "ready", "request": "use",
         "guard": [{"terms": [{"reg": "credits"}], "op": ">=", "const": 1}],
         "update": {"credits": {"kind": "sub", "value": 1}},
         "response": {"status": 200, "label": "consumed"}}
      ]
    },
    "new": {
      "initial_state": "boot",
      "registers": {"credits": 0},
      "transitions": [
        {"id": "n0", "from": "boot", "to": "ready",
         "update": {"credits": {"kind": "const", "value": 1}}},
        {"id": "n1", "from": "ready", "to": "ready", "request": "use",
         "guard": [{"terms": [{"reg": "credits"}], "op": ">=", "const": 1}],
         "update": {"credits": {"kind": "sub", "value": 1}},
         "response": {"status": 200, "label": "consumed"}}
      ]
    }
  }'
```

响应（HTTP 200，节选）：

```json
{
  "compatible": false,
  "counterexample": {
    "length": 2,
    "trace": [
      {"request": "use", "param": 0, "status": 200, "label": "consumed"},
      {"request": "use", "param": 0, "status": 200, "label": "consumed"}
    ],
    "old_path": [
      {"kind": "tau", "id": "t0", "from": "boot", "to": "ready",
       "registers": {"credits": 2}},
      {"kind": "request", "id": "t1", "from": "ready", "to": "ready",
       "request": "use", "param": 0, "status": 200, "label": "consumed",
       "registers": {"credits": 1}},
      {"kind": "request", "id": "t1", "from": "ready", "to": "ready",
       "request": "use", "param": 0, "status": 200, "label": "consumed",
       "registers": {"credits": 0}}
    ],
    "new_reachable": [
      [{"state": "boot", "registers": {"credits": 0}}],
      [{"state": "ready", "registers": {"credits": 0}}],
      []
    ]
  }
}
```

### 3. 兼容：仅声明已检验边界

旧版 `param ≤ 100 → 200 accepted`，否则 `413 too_large`；新版把受理阈值放宽到
200，其余一致：

```bash
curl -s -X POST http://localhost:8000/compare \
  -H 'Content-Type: application/json' \
  -d '{
    "old": {
      "initial_state": "s0", "registers": {},
      "transitions": [
        {"id": "t0", "from": "s0", "to": "s0", "request": "order",
         "guard": [{"terms": [{"param": true}], "op": "<=", "const": 100}],
         "response": {"status": 200, "label": "accepted"}},
        {"id": "t1", "from": "s0", "to": "s0", "request": "order",
         "guard": [{"terms": [{"param": true}], "op": ">", "const": 100}],
         "response": {"status": 413, "label": "too_large"}}
      ]
    },
    "new": {
      "initial_state": "s0", "registers": {},
      "transitions": [
        {"id": "n0", "from": "s0", "to": "s0", "request": "order",
         "guard": [{"terms": [{"param": true}], "op": "<=", "const": 200}],
         "response": {"status": 200, "label": "accepted"}},
        {"id": "n1", "from": "s0", "to": "s0", "request": "order",
         "guard": [{"terms": [{"param": true}], "op": ">", "const": 100}],
         "response": {"status": 413, "label": "too_large"}}
      ]
    }
  }'
```

响应（HTTP 200）：

```json
{
  "compatible": true,
  "checked_requests": 8,
  "detail": "Every request/response trace of the old model with up to 8 request(s) is reproducible by the new model. This is a bounded result only; behaviour beyond 8 requests was not checked."
}
```

### 4. 非法模型 → 422

```bash
curl -s -X POST http://localhost:8000/compare \
  -H 'Content-Type: application/json' \
  -d '{"old": {"initial_state": "s0", "registers": {"a": 0, "b": 0, "c": 0},
        "transitions": []},
       "new": {"initial_state": "s0", "registers": {}, "transitions": []}}'
```

响应（HTTP 422）：

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "old", "registers"],
      "msg": "Value error, at most two registers are allowed",
      "input": {"a": 0, "b": 0, "c": 0},
      "ctx": {"error": {}}
    }
  ]
}
```

## 实现要点

- **符号化有界模型检测**：寄存器与每轮请求参数 `p_0…p_7` 都是 z3 整数变量，
  配置集合表示为“符号化运行”（控制状态 + 关于参数的寄存器表达式 + 路径约束），
  因此对 32 位参数空间**不做枚举**；
- 每个 BFS 层先对整层做发散查询（旧版可走而新版不可走的参数区间是否存在），
  发现即得该长度下的所有候选反例，再用 `z3.Optimize` 按字典序最小化参数向量，
  保证输出**最短且字典序最小**的反例；
- **CI 性能**：单阈值 32 位参数模型实测约 30ms 出结果（要求 10s 内），
  引擎另设 25s 时间预算与节点/运行数上限，超限返回 503 而非错误结论。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

覆盖：恒等模型兼容、阈值分叉的最短/字典序最小反例、同一请求的不同响应、
τ 步不可观测性、每次请求前至多两次 τ、越界更新不可走、跨请求参数传递比较、
非法模型 422、单阈值 32 位模型 10 秒内出结果。

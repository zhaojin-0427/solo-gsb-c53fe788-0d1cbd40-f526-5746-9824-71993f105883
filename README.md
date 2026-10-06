# 有状态接口升级兼容性检查服务

基于 FastAPI 与 z3-solver 的协议审查服务：比较同一接口的**旧版**与**新版**状态模型，
检查旧版在至多 **8 次请求**内产生的每一条「请求/响应」轨迹（包括同一请求的不同响应）
能否被新版重现。

- 失败时返回**最短反例**（轨迹长度最小；同长度按「请求名 → 参数 → 状态码 → 标签」的
  轨迹字典序取最小），并给出旧版执行路径与新版逐步可达状态；
- 成功时仅声明已检验的请求边界（有界结论，不外推）；
- 模型非法时返回 **422**；
- 供 CI 使用的单阈值 32 位参数模型在 10 秒内给出确定结果。

## 快速启动

### Docker Compose（一键启动）

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`。

### 本地开发

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest tests/
```

## 访问方式

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/compare` | 提交旧/新状态模型，返回兼容性结论 |
| `GET` | `/healthz` | 存活探针 |
| `GET` | `/docs` | 交互式 OpenAPI 文档 |

## 模型格式

`POST /compare` 的请求体：

```json
{
  "old": { "<状态模型>" },
  "new": { "<状态模型>" },
  "max_requests": 8
}
```

`max_requests` 可选，取值 1–8，默认 8（即检查八次请求内的所有轨迹）。

状态模型：

```json
{
  "initial_state": "idle",
  "registers": {"balance": 2},
  "transitions": [ "<迁移>" ]
}
```

- `registers`：至多 **两个** 32 位无符号整数寄存器，键为寄存器名，值为初始值
  （`0 .. 2^32-1`）。可以没有寄存器。
- `initial_state`：初始控制状态（任意非空字符串）。

迁移分两种：

**请求迁移**（含 `request` 字段）——消费一个具名请求（携带一个 32 位无符号整数
参数），并返回状态码与标签：

```json
{
  "id": "charge-ok",
  "from": "idle",
  "to": "idle",
  "request": "charge",
  "guard": [
    {"terms": [{"reg": "balance"}], "op": ">", "const": 0},
    {"terms": [{"param": true}], "op": "<=", "const": 100}
  ],
  "update": {"balance": {"sub": 1}},
  "response": {"status": 200, "label": "charged"}
}
```

**τ（tau）迁移**（不含 `request` 字段）——无输入输出的静默步，不得定义
`response`，守卫与更新都不得引用请求参数：

```json
{"id": "reset", "from": "open", "to": "open",
 "guard": [{"terms": [{"reg": "uses"}], "op": ">=", "const": 3}],
 "update": {"uses": {"set": 0}}}
```

字段说明：

- `guard`：线性比较的合取列表（可省略，默认为真）。每个原子为
  `sum(coef * term) <op> const`，其中 `term` 为 `{"reg": 名称}` 或
  `{"param": true}`（均可带整数 `coef`，默认 1），`op ∈
  {"<=", "<", ">=", ">", "==", "!="}`，`const` 为整数。
- `update`：按寄存器名给出更新，每个寄存器恰好一种：
  `{"set": c}`（赋常数）、`{"param": true}`（取参数）、`{"add": c}`、
  `{"sub": c}`（加减常数，`c` 为 `0 .. 2^32-1`）。未列出的寄存器保持不变。
- `response`：`{"status": 100..599, "label": 非空字符串}`，仅请求迁移可带且必须带。
- `id`：可选，用于在结果路径中标识迁移。

## 执行语义

- 一次**请求周期** = 至多两次 τ 步，随后恰好一次请求迁移（每次请求前至多两次 τ）。
- 参数：每次请求携带一个 `0 .. 2^32-1` 的整数。
- **越界更新不可走**：若更新结果超出 32 位无符号范围，该迁移不可执行。
- 轨迹 = 请求周期序列的可观察部分 `(请求名, 参数, 状态码, 标签)`。
- 兼容性 = 旧版在边界内的每条轨迹都能被新版重现（旧版非确定性产生的不同响应
  各自都要可重现；新版允许有额外行为）。

## 响应

成功（仅声明已检验边界）：

```json
{
  "result": "compatible",
  "checked_requests": 8,
  "detail": "Every request/response trace of the old model with up to 8 request(s) is reproducible by the new model. This is a bounded result only: behaviour beyond 8 request(s) was not checked."
}
```

失败：返回最短反例 `counterexample.trace`、旧版路径 `old_path`、新版逐步可达状态
`new_reachable`（见下方真实示例）。

模型非法：`HTTP 422`，`detail` 中给出具体字段错误。

求解超时等无法判定的情况：`HTTP 503`。

## 真实请求/响应示例

以下示例由运行中的服务实际产生（示例文件见 `examples/`）。

### 例 1：检出升级回归（新版把限额阈值从 100 改为 99）

```bash
curl -s -X POST http://localhost:8000/compare \
  -H 'Content-Type: application/json' \
  -d @examples/incompatible.json
```

请求体（节选，完整见 `examples/incompatible.json`）：旧版 `charge` 在
`balance > 0 且 param <= 100` 时响应 `200 charged`，新版把阈值收紧为 `99`。

响应（`HTTP 200`）：

```json
{
  "result": "incompatible",
  "checked_requests": 8,
  "counterexample": {
    "length": 1,
    "trace": [
      {"request": "charge", "param": 100, "status": 200, "label": "charged"}
    ]
  },
  "old_path": {
    "initial": {"state": "idle", "registers": {"balance": 2}},
    "steps": [
      {
        "kind": "request",
        "transition": "charge-ok",
        "request": "charge",
        "param": 100,
        "response": {"status": 200, "label": "charged"},
        "from": "idle",
        "to": "idle",
        "registers_after": {"balance": 1}
      }
    ]
  },
  "new_reachable": [
    {
      "step": 1,
      "request": "charge",
      "param": 100,
      "expected_response": {"status": 200, "label": "charged"},
      "response_reproduced": false,
      "states_before_request": [
        {"state": "idle", "registers": {"balance": 2}}
      ],
      "new_possible_responses": [
        {"status": 402, "label": "too-expensive"}
      ],
      "states_after_request": [
        {"state": "idle", "registers": {"balance": 1}}
      ]
    }
  ]
}
```

### 例 2：兼容升级（新版仅新增请求类型，含 τ 重置步）

```bash
curl -s -X POST http://localhost:8000/compare \
  -H 'Content-Type: application/json' \
  -d @examples/compatible.json
```

响应（`HTTP 200`）：

```json
{
  "result": "compatible",
  "checked_requests": 8,
  "detail": "Every request/response trace of the old model with up to 8 request(s) is reproducible by the new model. This is a bounded result only: behaviour beyond 8 request(s) was not checked."
}
```

### 例 3：非法模型（三个寄存器）

```bash
curl -s -X POST http://localhost:8000/compare \
  -H 'Content-Type: application/json' \
  -d '{"old": {"initial_state": "s", "registers": {"x": 0, "y": 0, "z": 0}, "transitions": []},
       "new": {"initial_state": "s", "registers": {}, "transitions": []}}'
```

响应（`HTTP 422`）：

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "old"],
      "msg": "Value error, at most two 32-bit registers are allowed",
      "input": {"initial_state": "s", "registers": {"x": 0, "y": 0, "z": 0}, "transitions": []},
      "ctx": {"error": {}}
    }
  ]
}
```

## 实现说明

- 检查算法只使用量词自由的线性整数算术：对每个轨迹长度 `k = 1..max_requests`，
  先把新版做 k 步**符号化正向探索**——寄存器符号值只有「常量」或
  「`param_i + 常量`」两种形态，按（控制状态, 符号寄存器）合并配置——得到精确刻画
  新版可重现轨迹的公式 `M`；再用 z3 Optimize 在「旧版轨迹 ∧ ¬M」上求
  （请求名, 参数, 状态码, 标签）字典序最小的解。有解即最短且字典序最小的反例，
  无解则该长度通过。
- 单阈值 32 位参数模型（CI 场景）的判定在毫秒级完成，远低于 10 秒要求；
  单个求解器查询的时间上限为 10 秒，超时返回 503。

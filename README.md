# UDP 线路载荷测量服务

采集线路改路后，值班员需要知道当前线路上 UDP 包还能发多大。本服务提供测量接口：
给定载荷字节数的上下限，通过**实际 UDP 收发**二分探测出范围内可用的最大值，
探测次数为 **O(log 范围宽度)**，不随范围线性增长。

自带两条可切换的本地回显线路（模拟器），可模拟**超长丢包、回包延迟、断线**；
测量期间可切线，切线立即撤下旧建议，即使切回原线路，也只接受切换后开始的测量结果。
**阈值可在线调整**：改 `max_size` 立即撤下旧建议、作废改动前启动的测量，不必等
测量结束；配置结果以回显端实际生效情况为准，无法确认时该线路暂停提供建议。

## 组成

| 组件 | 说明 | 端口（compose 内） |
|---|---|---|
| `app` | FastAPI 测量服务（本仓库 `app/`） | `8000`（映射到宿主机） |
| `echo-a` | 回显线路 a：默认超过 1500B 丢弃、延迟 30ms | UDP `9001` / 控制口 `9101`（已映射） |
| `echo-b` | 回显线路 b：默认超过 4000B 丢弃、延迟 80ms | UDP `9002` / 控制口 `9102`（已映射） |

回显线路逐字节回显；超过 `max_size` 的包丢弃（超长丢包），`delay_ms` 后回包
（回包延迟），`down=true` 时全部丢弃（断线）。行为以收到包那一刻的配置为准。
回显端另维护单调递增的 `threshold_version`：`max_size` 实际变化（含改回原值）才 +1。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

无 Docker 时也可本地跑（Python 3.11+）：

```bash
pip install -r requirements.txt
LINE_NAME=a UDP_PORT=9001 CTRL_PORT=9101 MAX_SIZE=1500 DELAY_MS=30 python -m echo.server &
LINE_NAME=b UDP_PORT=9002 CTRL_PORT=9102 MAX_SIZE=4000 DELAY_MS=80 python -m echo.server &
uvicorn app.main:app --port 8000
```

## 在 /docs 上演示测量与切线

1. **测量**：`POST /api/measure`，Body `{"lower": 512, "upper": 9000}` → 202 返回测量
   任务；用返回的 `id` 调 `GET /api/measurements/{id}` 查看进度与逐包事件轨迹
   （二分过程、三次重试、最短包探活都记录在 `events` 里）。
2. **看建议**：`GET /api/recommendation` → `max_payload: 1500`（线路 a）。
3. **切线**：`POST /api/line/switch`，Body `{"line": "b"}` → 响应里
   `recommendation` 立即为 `null`；再测一次 → 线路 b 测出 `4000`。
4. **模拟断线**：`POST /api/lines/b/config`，Body `{"down": true}` → 再发起测量，
   结果 `status: undetermined`（最短包连试三次也超时），本轮不发布建议。
   `{"down": false}` 恢复。
5. **测量中切线**：先发一个宽范围测量（如 `{"lower": 512, "upper": 65507}`），
   趁 `status` 还是 `running` 时切线（可切走再切回）→ 该测量以
   `aborted`/`discarded` 结束，建议保持为 `null`，需重新测量。
6. **测量中改阈值**：先发宽范围测量，趁 `status=running` 时
   `POST /api/lines/a/config`，Body `{"max_size": 2000}` → 响应里
   `threshold_changed: true`，`GET /api/recommendation` 立即为 `null`；
   在途测量随即以 `aborted` 结束且不发布。再 `{"max_size": 1500}` 改回原值，
   建议仍保持 `null`（“实际变过”即作废旧建议），需重新测量。
7. **调整备用线**：线路 a 为现用线时改线路 b 的 `max_size` → 只影响 b，
   现用线 a 的建议和在途测量照常。
8. **配置生效后响应超时**：可直接对回显控制口
   `POST http://localhost:9101/fault`，Body
   `{"post_applies_then_delay_ms": 4000, "get_delay_ms": 0}`（自测用故障注入），
   再 `POST /api/lines/a/config {"max_size": 2400}` → 主服务 3s 超时后回读回显端
   实际配置，响应 `outcome: reconciled`、`threshold_changed: true`，旧建议撤下。
   若连回读也超时（`get_delay_ms` 同样调大）→ 返回 503、该线路
   `recommendation_status: config_unconfirmed`，暂停提供/发布建议；恢复控制口后
   按回显端实况自动恢复或作废旧建议。演示完把两个值调回 `0`。

## 阈值在线调整语义

- 回显端为每条线路维护单调递增的 `threshold_version`：只有 `max_size`
  **实际变化**才 +1，**改回原值同样 +1**（因此“改过又改回”也会撤下旧建议）。
- 主服务为每条线路维护独立 `generation`：确认阈值版本变化即 +1，并**立即撤下
  该线路旧建议**——调整阈值不必等测量结束。
- 测量启动时记录 `epoch + generation`；测量中或发布前发现任一变化即中止/作废，
  改动前启动的测量**一律不得发布**。
- 配置结果以回显端实际生效状态为准：配置请求超时/连接异常时，主服务回读
  `/config` 对账——真变了就撤旧建议，没变则原建议与在途测量继续可用。
- 配置结果无法核实（配置与回读都失败）时该线路 `confirmed=false`：暂停提供建议、
  暂停发布测量结果；之后核实阈值其实没变，原建议与在途测量可继续使用。
- 所有状态按线路隔离：调整备用线不影响现用线的建议和在途测量。

## 测量算法与判定规则

- 在 `[lower, upper]` 上**二分**：中点可用则搜右半，不可用则搜左半。
- 某长度**连试 3 次**（次数可调）都收不到匹配回包时，不直接判死——改发范围内
  **最短包**（`lower` 字节）确认线路仍通：
  - 最短包有匹配回包 → 该长度才判为不可用；
  - 最短包连试 3 次也超时 → 返回**无法判定**（`undetermined`），本轮不发布建议。
- 回包按**内容逐字节匹配**（每包随机载荷），串台包、迟到包不计数。
- 探测次数：每轮二分最多 `3(试) + 3(探活)` 个包，总复杂度 O(log 宽度)。

## 切线语义

- 每次真实切线（目标 ≠ 当前线路）：`epoch + 1`，**立即撤下旧建议**。
- 测量启动时记录 `epoch`；测量中或发布前发现 `epoch` 变化即中止/作废。
- 因此**切回原线路也没用**：a→b→a 之后，只接受最后一次切换之后开始的测量。

## API 一览

| 方法/路径 | 说明 |
|---|---|
| `POST /api/measure` | 发起测量（`lower`/`upper`/`timeout_ms`/`attempts`），202 返回任务 |
| `GET /api/measurements/{id}` | 测量进度、结果、逐包事件 |
| `GET /api/measurements` | 最近测量列表 |
| `GET /api/recommendation` | 当前建议（无则 `null`）；含 `recommendation_status` 与各线路 `line_config` |
| `GET /api/line` / `POST /api/line/switch` | 当前线路 / 切线 |
| `GET /api/lines` | 两条线路的实时配置（含 `threshold_version`）、配置确认状态与收发统计 |
| `POST /api/lines/{name}/config` | 调模拟参数：`max_size`/`delay_ms`/`down`；响应含 `threshold_changed`/`generation`/`outcome` |
| `GET /docs` | Swagger 交互式文档 |

回显模拟器另有自测用控制口：`POST /fault`（`post_applies_then_delay_ms`/
`get_delay_ms`），用于注入“配置已生效但响应超时”“控制口不可达”等故障。
compose 已把 `9101`/`9102` 映射到宿主机。

## 自测

```bash
pip install -r requirements-dev.txt
pytest                                  # 25 项单元/集成测试
# 对运行中的服务栈（compose 或本地）跑端到端场景：
python3 scripts/e2e.py --base http://127.0.0.1:8000 \
    --ctrl-a http://127.0.0.1:9101 --ctrl-b http://127.0.0.1:9102
```

## 实测结果（2026-11-22，本机 Python 3.11 跑完整服务栈）

- `pytest`：**25/25 通过**（引擎级 7 项 + API 级 7 项 + 阈值在线调整 11 项）。
- `scripts/e2e.py`：**45/45 项通过**，关键数据：
  - 超长丢包：线路 a（阈值 1500）测 `[512, 9000]` → `max_payload=1500`，
    **仅发 31 个探测包**（范围宽 8489，线性需 8489 次）；
  - 断线（`down=true`）：测量返回 `undetermined`（“最短包 512B 连续3次超时，
    线路疑似中断”），旧建议保留、未发布新建议；
  - 测量中切线及 a→b→a 切回：旧测量均 `aborted`，建议立即为 `null`，
    切换后重新测量才发布；
  - **探测收发间改阈值**：测量 `running` 时把 a 的 `max_size` 1500→2000，
    配置响应即 `threshold_changed=true` 且建议立即为 `null`；该测量
    `aborted`（detail“测量期间线路阈值配置实际变化”），重测得到 2000；
  - **改回原值**：2000→1500→2000 两次 `threshold_changed=true`，建议保持
    `null`，改动前启动的测量仍 `aborted`，重测得到 2000；
  - **调整备用线**：现用线 a 在途测量期间把 b 改成 5000，a 的建议仍在、
    在途测量 `completed` 并发布；
  - **配置生效后响应超时**：回显端让 `max_size=2400` 生效后把响应拖到 4s，
    主服务 3s 超时后回读对账 → 200、`outcome=reconciled`、
    `threshold_changed=true`，旧建议撤下，重测得到 2400；
  - **配置与控制口都超时**：返回 503、`recommendation_status=config_unconfirmed`，
    暂停提供建议；恢复后确认阈值已变 → 旧建议作废，重测得到 2600；
  - 线路统计佐证：线路 a `dropped_oversize=40`，线路 b `dropped_down=6`。
- Docker 本环境不可用，`docker-compose.yml` 已做结构校验（3 个服务、命令与
  环境变量与代码一致，控制口已映射），镜像内启动命令与本地实测命令相同。

## 余项 / 已知限制

- 测量为**单并发**（物理线路共享，避免互相干扰）；并发请求返回 409。
- 建议与测量历史只存内存，服务重启即清空；未做持久化与鉴权（内网值班工具定位）。
- 回显按全量字节比对，未做分片校验和等优化；单包最大 65507B（UDP 协议上限）。
- 大于 MTU 的 UDP 包依赖 IP 分片送达；跨真实广域网时路径可能丢分片，
  本工具面向改路后的本地回显线路验证，不替代 RFC 8899 之类的 PMTUD。
- 断线期间发起的测量返回“无法判定”后，旧建议按约定保留；如需更保守的
  “断线即撤建议”，可在 `LineManager` 增加线路健康钩子。
- 阈值变化只在能读到回显端 `threshold_version` 时才算“确认”。若使用不带该字段的
  旧回显端，则退化为比较 `max_size` 实值（无法识别“改走又改回同一值”）。
- 回显端重启会使 `threshold_version` 归零；主服务检测到版本回退会自动改用
  `max_size` 实值比较兜底，但服务自身重启仍会清空内存状态。
- 配置未确认期间只暂停**该线路**的建议提供与发布，不会自动重试配置请求；
  值班员重试配置或等待控制口恢复后查询即可自动核对。

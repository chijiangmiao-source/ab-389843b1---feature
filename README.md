# 深空回绕时标因果审计服务 (wrap-audit)

地面站汇集多个设备的回绕时标（模 `M` 计数器读数）。本服务判定一组带因果窗口的
记录能否落在同一条真实时间线上，并给出可复算的证据。**相同的计数值不会被视作
同一时刻**——每个事件的绝对 tick 为

```
t_e = counter_e + M * k_e ,   k_e ∈ ℤ, k_e ≥ 0   (回绕次数，计数器自纪元 0 起)
```

锚点事件的绝对 tick 已知（无回绕变量）。约束 `src → dst = [lo, hi]` 表示闭区间
`lo ≤ t_dst − t_src ≤ hi`。全部事件必须经约束（无向）连通锚点，否则请求被 400
拒绝。回绕次数以整数精确展开（全程整数运算，无浮点）。

## 判定结论

| status     | 含义 | 证据 |
|------------|------|------|
| `unique`   | 唯一时间线 | 每事件回绕次数 + 从锚点出发的推导链 |
| `multiple` | 多解 | 前两条规范时间线（按事件标识字典序最小的回绕向量）+ 首个不稳定先后关系 |
| `unsat`    | 无解 | 可复算的冲突约束链（下界推导 vs 上界推导，含 k 空间与 tick 空间数值） |

示例：`M=100, 锚点 A=95, B=3, A→B=[8,8]` ⇒ `unique`，`B` 唯一展开为 **103**
（`k_B = 1`）。

首个不稳定先后关系：按事件标识顺序（锚点最前）找到的第一对 `(a, b)`，其在所有
解中 `t_a − t_b` 的精确范围跨零（先后关系随解翻转）；证据含两条规范时间线中的
实际差值。无解链示例：`A→B=[8,8]`（`t_B=103`）与 `B→A=[92,92]`（`t_B=3`）⇒
`k_B ≥ 1` 与 `k_B ≤ 0` 冲突，证据给出两条推导链及每步所用的约束。

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health` | 健康检查，`{"status":"ok"}` |
| POST | `/audits` | 创建审计：`201` 新建 / `200` 幂等重放 / `400` 载荷非法 / `409` 同标识不同载荷 |
| GET  | `/audits/{no}` | 按编号读取冻结的输入、结论与证据 |
| POST | `/audits/{no}/repairs` | 对冻结的 **unsat** 审计发起最小窗口放宽修复：`201` 新建 / `200` 幂等重放 / `400` 载荷非法 / `404` 来源审计不存在 / `409` 来源不是 unsat 或修复标识复用改载荷 |
| GET  | `/repairs/{no}` | 按编号读取冻结的修复：来源快照、最小代价、规范时间线与逐约束证据 |

POST 载荷：

```json
{
  "request_id": "req-001",
  "modulus": 100,
  "anchor": {"id": "A", "tick": 95},
  "events": [{"id": "B", "counter": 3}],
  "constraints": [{"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]}]
}
```

* 事件至多 12 个；`counter ∈ [0, M)`；约束窗口为整数闭区间 `[lo, hi]`；
  端点为事件标识或 `"anchor"`。
* **幂等**：相同 `request_id` + 相同载荷（规范化 JSON 的 SHA-256 一致）重传
  返回原审计编号（`200, replayed=true`）；任一事件或约束改动 ⇒ `409` 拒绝且
  不新增记录。记录持久化于 SQLite（`AUDIT_DB`，默认 `/data/audits.db`）。

## 最小窗口放宽修复（repair）

审计员读取一条 `unsat` 审计后，可用修复标识发起修复。服务只**读取并冻结**
该无解记录的输入与冲突证据，来源审计及其读取结果永不被改写；随后对称放宽
各因果窗口（整数 tick，物理约束 `k_e ≥ 0` 不放宽）：

```
c: [lo, hi]  ->  [lo − x_c, hi + x_c],  x_c ∈ ℤ, x_c ≥ 0
```

在事件计数模数 `M` 与非负回绕次数约束下**精确最小化** `Σ_c x_c`，并返回：

* `total_extension`：最小总扩展量（tick）；
* `timeline`：落入同一真实时间线的规范时间线（每事件回绕次数 + 绝对 tick）；
* `constraints`：逐约束的**原窗口**、放宽后窗口、扩展量、放宽方向
  （`lower`/`upper`/`none`，即时间线落在原窗口的哪一端之外）与**复算后的差值**；
* `canonical_decision`：约束标识序列上的放宽向量与事件标识序列上的回绕向量。

**同代价裁决**：总扩展量相同的方案，先按约束标识序列裁决放宽向量，再按
事件标识序列裁决时间线（均取字典序最小）。优化目标是有序回绕标签上的
凸/子模分段线性能量（每条约束的代价是 `k_dst − k_src` 的双斜坡函数），
用一次整数最小割（Ishikawa 构造）精确求解；字典序裁决以混合进制大整数
权编码进割容量。标签域由"按可行种子代价统一放宽后传播出的紧界"给出，
包含全部不劣于种子的时间线。

修复请求体只携带修复标识（其他字段一律 `400`，防止复用标识夹带改载荷）：

```bash
curl -XPOST localhost:8080/audits/7/repairs \
  -H 'Content-Type: application/json' -d '{"repair_id":"fix-001"}'
```

* 相同 `repair_id` + 相同载荷（且同一来源审计）重传 ⇒ `200` 返回**原修复
  编号**；复用 `repair_id` 改变载荷或换来源 ⇒ `409`/`400`，不新增结果。
* `GET /repairs/{no}` 返回冻结的来源输入、原 unsat 冲突证据、最小代价与
  修复证据；SQLite 持久化，**服务重启后仍可按编号核对**。

## 运行

```bash
# 本地
PORT=8080 AUDIT_DB=/tmp/audits.db python3 -m app.service

# Docker（宿主机端口可配置，默认 8080）
HOST_PORT=9090 docker compose up --build audit
curl localhost:9090/health
```

## 验收（一次性 verify 服务）

```bash
docker compose up --build --abort-on-container-exit --exit-code-from verify verify
```

`verify` 待 `audit` 健康后执行：① 构建检查（全部源码字节码编译）② 代码测试
（求解器 + 修复求解器 + API 单元测试）③ 围绕唯一展开、歧义双时间线、
双向矛盾链、幂等记录，以及最小窗口放宽修复（单窗口修复、同代价候选的规范
裁决、非法来源拒绝、修复幂等与冻结读取、原审计兼容读取）的 API/HTTP 冒烟；
全部结束后以退出码报告验收结果（0=PASS）。

无 Docker 时等价本地验收：

```bash
python3 -m compileall -q app tests verify   # 构建检查
python3 -m unittest discover -s tests -v    # 代码测试
PORT=8080 AUDIT_DB=/tmp/a.db python3 -m app.service &
BASE_URL=http://127.0.0.1:8080 python3 verify/smoke.py
```

## 求解方法（整数差分约束）

约束代入 `t_e = c_e + M·k_e` 后化为 `k` 空间的整数差分约束（边界除以 `M` 时
精确 ceil/floor 取整），外加合成纪元节点 `Z=0` 上的 `k_e ≥ 0`：

```
anchor→e [lo,hi] :  ceil((lo+A−c_e)/M) ≤ k_e ≤ floor((hi+A−c_e)/M)
e→anchor [lo,hi] :  ceil((lo+c_e−A)/M) ≤ −k_e ≤ floor((hi+c_e−A)/M)
s→d      [lo,hi] :  ceil((lo+c_s−c_d)/M) ≤ k_d−k_s ≤ floor((hi+c_s−c_d)/M)
```

下/上界同步松弛（最长/最短路传播）至不动点：出现 `low > high` 或正权环即无解，
并沿前驱链输出可复算证据。所有变量界有限 ⇒ 解集有限：全变量 `low == high` 即
唯一；否则用前缀固定 + 重传播取字典序最小/次小可行向量（固定前缀下每变量可行
值是连续整数区间），成对 tick 差的精确范围由可行性二分搜索求得。

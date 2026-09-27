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
（求解器 + API 单元测试）③ 围绕唯一展开、歧义双时间线、双向矛盾链、幂等记录
的 API/HTTP 冒烟；全部结束后以退出码报告验收结果（0=PASS）。

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

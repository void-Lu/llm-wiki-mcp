# Louvain 社区检测集成规格

**日期**：2026-05-26
**优先级**：P1（高价值、低复杂度）
**影响模块**：`wiki_insights.py`

---

## 问题

当前 `wiki_insights.py` 的 `_detect_communities` 使用连通分量（connected components）作为社区检测。对于连通图，所有节点归入同一社区，导致：
- 桥接页检测失效（无跨社区边）
- 意外连接评分无意义（所有节点同社区）
- 无法发现知识簇的内部结构

## 方案

实现 Louvain 算法（无外部依赖，保持 `mcp` + `PyYAML` 最小依赖原则）。

### Louvain 算法概要

1. **初始化**：每个节点独立社区
2. **Phase 1（局部移动）**：遍历节点，将每个节点移入使模块度增益最大的邻居社区；重复直到无改进
3. **Phase 2（聚合）**：将社区折叠为超节点，构建新图
4. **迭代**：重复 Phase 1 + 2 直到收敛

模块度增益公式：
```
ΔQ = k_i_in / m - Σ_tot * k_i / (2m²)
```

### 输入

- 无向加权图（节点 = wiki 页面 rel path，边 = wikilink，权重 = 1.0）

### 输出

- `community_map: dict[str, int]` — 节点到社区 ID 的映射
- `communities: list[set[str]]` — 社区成员列表
- `modularity: float` — 最终模块度 Q

## 集成点

### wiki_insights 增强

1. 替换 `_detect_communities` → 调用 Louvain
2. 新增 insight 类型：
   - `communities_summary`：社区数量、平均大小、模块度
   - `sparse_community`：内聚度 < 0.15 的社区（内部边数 / 可能边数）
3. 改进 `bridge_page`：基于 Louvain 社区而非连通分量
4. 改进 `surprising_connection`：跨 Louvain 社区的边更有意义

### API 变更

`wiki_insights()` 返回新增字段：
```python
{
    "communities": [
        {"id": 0, "size": 5, "cohesion": 0.8, "members": [...]},
        ...
    ],
    "modularity": 0.42,
}
```

## 不做的事

- 不引入外部依赖（`python-louvain` / `networkx`）
- 不修改 `wiki_query.py`（4-Signal 已实现）
- 不做加权边（保持 wikilink = 1.0，简单有效）

## 验收标准

1. 对连通图能检测出多个社区（非退化为单社区）
2. 模块度 Q > 0 对有结构的图
3. 桥接页检测在连通图中正常工作
4. 现有测试继续通过
5. 新增 Louvain 专项测试

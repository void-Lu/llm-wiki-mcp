---
type: script
script_type: mapreduce
project: project-a
author: developer-name
script_id: customscript_example_mr
script_name: Example Map/Reduce
deployment_id: customdeploy_example_mr
related_objects: [salesorder]
related_scripts: []
status: active
tags: [netsuite, mapreduce]
---

# Map/Reduce - Example Map/Reduce

## 关联需求
- 禅道: []

## 用途
说明 Map/Reduce 解决的业务问题：批量处理大量数据记录。

## 入口函数
| 函数 | 说明 |
|---|---|
| getInputData | 获取输入数据（搜索结果或数组） |
| map | 对每条输入数据执行映射操作 |
| reduce | 对映射结果执行聚合操作 |
| summarize | 汇总处理结果、记录错误 |

## 核心逻辑
1. getInputData: 查询待处理记录。
2. map: 逐条处理，提取关键字段。
3. reduce: 按组聚合，执行批量操作。
4. summarize: 输出统计、记录错误。

## 部署配置
- Script ID: `customscript_example_mr`
- Deployment ID: `customdeploy_example_mr`

## 关联对象
- salesorder

## 相关脚本
- 无

## 性能注意事项
- ⚠️ map/reduce 每个阶段有治理限制
- ⚠️ 大数据集分批处理，避免单次过多
- ⚠️ 使用 N/runtime 获取剩余治理点数

## 排坑记录
- 暂无
---
type: object
object_type: deployment
project: project-a
object_id: customdeploy_example
related_objects: [salesorder]
related_scripts: [customscript_example_ue]
status: active
tags: [netsuite, deployment]
---

# Deployment - Example Script Deployment

## 关联需求
- 禅道: []

## 业务目的
说明该脚本部署的配置细节和运行参数。

## 部署配置
| 配置项 | 值 |
|---|---|
| Script ID | customscript_example_ue |
| Deployment ID | customdeploy_example |
| 记录类型 | Sales Order |
| 执行角色 | Administrator |
| 状态 | Released |

## 执行日志
- 部署日期: 2026-01-01
- 执行频率: 每次 afterSubmit

## 维护注意事项
- 修改部署参数需同步更新笔记
- 禁用脚本部署前确认无业务依赖

## 排坑记录
- 暂无
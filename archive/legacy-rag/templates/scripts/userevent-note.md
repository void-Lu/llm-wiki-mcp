---
type: script
script_type: userevent
project: project-a
author: developer-name
script_id: customscript_example_ue
script_name: Example UserEvent
deployment_id: customdeploy_example_ue
related_objects: [salesorder]
related_scripts: []
status: active
tags: [netsuite, userevent]
---

# UserEvent - Example UserEvent

## 关联需求
- 禅道: []

## 用途
说明 UserEvent 解决的业务问题：在记录操作前后执行自定义逻辑。

## 入口函数
| 函数 | 触发时机 | 说明 |
|---|---|---|
| beforeLoad | 加载前 | 动态修改表单 |
| beforeSubmit | 提交前 | 数据校验与转换 |
| afterSubmit | 提交后 | 触发后续流程 |

## 核心逻辑
1. 判断执行上下文（UI/API/批量）。
2. 根据触发时机执行对应逻辑。
3. 调用 NetSuite API 操作相关记录。
4. 如需触发后续脚本，使用 N/task 提交。

## 部署配置
- Script ID: `customscript_example_ue`
- Deployment ID: `customdeploy_example_ue`

## 关联对象
- salesorder

## 相关脚本
- 无

## 注意事项
- ⚠️ 注意避免在 beforeLoad 中使用 nlapiSubmitRecord（只读操作）
- ⚠️ afterSubmit 中避免循环触发

## 排坑记录
- 暂无
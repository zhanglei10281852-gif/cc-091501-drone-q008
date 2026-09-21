# 无人机适航管理服务

面向无人机巡检机队的适航管理服务：基于机体、可序列化部件、固件、维修工单、
检查签名、任务载荷与环境限制，计算指定时刻的有效配置与放行状态，条件不满足时
拦截任务；并保证部件履历连续、历史配置不可变、维修与任务占用互斥、紧急放行受控、
迟到日志补计后可追溯影响范围。

## 运行

需要 Python 3.11 或更高版本，无第三方依赖。

- `python src/index.py` 启动服务，默认监听 8000 端口（`PORT`/`HOST` 可覆盖）。
- `STORE_FILE=/path/to/store.json` 指定落盘位置；缺省为纯内存存储。
- `python -m unittest discover` 执行全部测试。
- `docker compose up --build` 启动容器。

## 角色（请求头 `X-Role`）

| 角色键 | 岗位 | 可见信息 |
| --- | --- | --- |
| `maintenance` | 机务人员 | 寿命与签名依据、配置、履历、工单、影响报告 |
| `dispatcher` | 调度员 | 可执行结论与阻断项（不含人员身份与寿命明细） |
| `release_officer` | 放行员 | 同机务视图 |
| `airworthiness_engineer` | 适航工程师 | 同机务视图，且是唯一可批准紧急放行的独立角色 |
| `regulator` | 监管人员 | 只读全量视图 |
| `readonly` | 只读用户 | 同调度视图 |

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 进程健康检查 |
| POST | `/airframes`、`/parts`、`/qualifications` | 登记机体/部件/人员资质 |
| POST | `/airframes/{id}/install`、`/remove`、`/firmware` | 装配履历与固件（任务占用期间禁止变更） |
| POST | `/parts/{serial}/life-sync` | 同步部件寿命（如换电池后补录循环数） |
| POST | `/airframes/{id}/inspections` | 检查签名（签署时即反馈签字人资质是否有效） |
| POST | `/work-orders`、`/work-orders/{id}/close` | 停场工单开立/关闭（占用机体） |
| POST | `/missions` | 创建任务（载荷与环境限制随任务提交） |
| GET | `/missions/{id}/release?at=` | 指定时刻放行评估，按角色投影 |
| POST | `/missions/{id}/dispatch` | 放行：通过则占用机体并固化配置快照，否则 409 拦截 |
| POST | `/flight-logs` | 飞行日志；迟到日志自动补计寿命并输出影响报告 |
| POST | `/emergency-releases` | 紧急放行（仅适航工程师，带失效时间与附加循环上限） |
| GET | `/airframes/{id}/config?at=`、`/history` | 指定时刻有效配置、部件连续履历 |

## 关键规则

- 阻断项包括：寿命未同步/超限、必检项缺失/过期/签字人资质失效、检查不通过、
  固件未批准、停场工单未关闭、占用冲突、载荷不兼容/超重、风速/温度/降水超限。
- 紧急放行只能覆盖文书/资质类阻断（寿命未同步、检查缺失/过期/签字无效），
  安全硬限制不可覆盖；批准人不得为申请人或涉事签字人。
- 迟到飞行日志按放行时的配置快照补计寿命，影响报告圈出受影响任务
  （含"曾超限飞行"标记）与复检范围（大修评估/定检/记录复核）。

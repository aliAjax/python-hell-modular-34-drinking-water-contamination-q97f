# 市政饮用水污染响应

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8334`。

- `app.py`：服务生命周期和依赖组装。
- `src/domain.py`：水源、污染物、区域和来源校验。
- `src/rules.py`：污染评分、通知去重、停水、切换水源、冲洗、消毒、复检和恢复状态机。
- `src/repository.py`：SQLite、事务、重复保护、乐观版本和审计链。
- `src/service.py`：身份、角色和用例编排。
- `src/http_api.py`：JSON 接口与静态首页。
- `src/audit.py`：审计哈希。

复检依据按受影响区域独立记录（`payload.zone_progress`），每个区域分别推进
**待冲洗 → 已消毒 → 已采样 → 已恢复**：

- 冲洗按 `zone_id` 记录；消毒必须提供 `batch_id` 和覆盖区域 `zone_ids`，一个批次可覆盖多个区域。
- 样本必须属于该区域**当前消毒批次**（同一 `batch_id`）且采样时间晚于本次消毒时间，否则不作为恢复依据。
- `restore` 不再接受总标记：所有区域都必须拥有当前合格样本（浓度 ≤ 限值），接口返回每个区域的 `stage`、`missing` 和全局 `restore_ready`/`zones_waiting`。
- 已恢复后再次登记某区域污染来源，或该区域复检样本超限：原恢复依据作废（进入 `restoration_history`），事件状态回到 `sampled`，旧样本失效，需在当前批次重新取得合格样本；其他区域的依据不受影响。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8334
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和审计查询。测试覆盖完整响应流程、按区域恢复门槛、消毒批次与采样时序、复检超限与再次登记来源导致的恢复依据作废、重复事件、重复通知、权限和版本冲突。内置规则不替代真实水质模型、法定通报渠道或供水控制系统的联锁。

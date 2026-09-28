# 市政饮用水污染响应

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8334`。

- `app.py`：服务生命周期和依赖组装。
- `src/domain.py`：水源、污染物、区域和来源校验。
- `src/rules.py`：污染评分、通知去重、停水、切换水源、冲洗、消毒、复检和恢复状态机。
- `src/repository.py`：SQLite、事务、重复保护、乐观版本和审计链。
- `src/service.py`：身份、角色和用例编排。
- `src/http_api.py`：JSON 接口与静态首页。
- `src/audit.py`：审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8334
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和审计查询。首页按区域展示复检进度与缺失项。测试覆盖完整响应流程、重复事件、重复通知、复检阈值、权限和版本冲突。内置规则不替代真实水质模型、法定通报渠道或供水控制系统的联锁。

## 按区域复检依据

恢复供水不再只看事件总标记，复检依据按每个受影响区域独立维护：

- 每个区域依次记录：**待冲洗 → 已消毒 → 已采样 → 已恢复**（冲洗后、消毒前显示“待消毒”）。
- `disinfect` 必须带 `batch_id`、`completed_at` 和 `zone_ids`，批次明确写明覆盖区域；被覆盖的区域必须已冲洗。
- `sample` 必须带 `batch_id` 和 `sampled_at`：样本只对同一批次、采样时间晚于该批次完成时间、且区域在批次覆盖范围内时有效；样本记录所属批次在各区域的依据编号（basis）。
- 区域的“当前合格样本”指：属于该区域当前依据下最新消毒批次、消毒后采集且浓度不超过限值的样本。
- 再次登记该区域的污染来源（`POST /sources` 带 `zone_id`），或该区域在当前批次下复检超限，该区域 basis 作废、旧样本失效；已恢复的区域回退到“已采样”，整个事件如已恢复也回到 `sampled`，需重新消毒采样。
- `restore` 不再接受 `all_zones_cleared` 标记：只有所有受影响区域都有当前合格样本时才能恢复，否则错误信息和 `GET /api/state` 的 `zone_progress[].missing_labels` 会列出每个区域还缺什么。

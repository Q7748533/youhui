# 供应商页面覆盖规则

## 目标
帮助用户快速判断供应商是否有可验证价格，并进入详情页。

## 页面结构
- 页面标题与数据范围说明；
- 状态筛选：Verified / No verified price / Source unavailable；
- 桌面端表格；
- 手机端供应商卡片；
- 官方站点链接；
- 最近验证日期。

## 供应商专属规则
- 状态必须同时显示文字和颜色；
- `No verified price` 不得替换成免费或价格估算；
- 官方站点链接使用 `rel="nofollow noopener"` 和新窗口；
- 详情页沿用 `/providers/<slug>/` URL；
- 保留 ItemList 和 BreadcrumbList JSON-LD。

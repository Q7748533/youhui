# 方案详情页覆盖规则

## 目标
让用户确认一个方案的当前价格、来源和验证时间，再决定是否访问官方页面。

## 页面结构
1. 面包屑与供应商名；
2. 方案名称；
3. 当前价格主卡；
4. Verified / Expired / Price not verifiable 状态；
5. 官方来源与验证时间；
6. 前往官方页面 CTA；
7. 缺失字段说明。

## 详情页专属规则
- 价格主视觉必须包含币种；
- 价格不可验证时不输出 Offer.price；
- CTA 文案使用 `View official page`；
- 不显示虚构折扣百分比；
- 保留 `/deals/<id>/` URL、Offer 和 BreadcrumbList JSON-LD。

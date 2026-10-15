# Cloud Storage Deals 全新用户界面设计方案

**版本：** 1.0  
**定位：** 面向需要比较云存储价格的个人、团队和备份用户  
**设计目标：** 让用户在 10 秒内理解站点价值，在 30 秒内找到合适的供应商和方案。

## 1. 核心判断

当前站点的数据与 SEO 基础已经具备，但界面仍偏“数据输出页”：

- 首页缺少明确的任务入口；
- 价格、更新时间、数据可信度层级不够突出；
- 供应商、方案、来源三类信息的层级容易混在一起；
- 对无法验证价格的供应商，用户不清楚这是“没有价格”还是“抓取失败”；
- 对比页在移动端需要更适合触摸操作的卡片化结构。

新界面采用 **“可信价格编辑台”** 方向：

> 先说明数据可信度，再帮助用户筛选价格，最后引导用户查看官方来源。

不做传统电商式强促销，不使用夸张折扣、虚构推荐或误导性按钮。

## 2. 信息架构

```text
首页
├── 任务入口：找便宜方案 / 找大容量 / 找团队方案
├── 可信数据摘要：供应商数、已验证方案、最近更新时间
├── 精选方案列表：按价格、容量、用途筛选
├── 供应商入口
└── 数据方法与更新说明

供应商
├── 供应商总览
├── 状态筛选：有验证价格 / 暂无验证价格 / 不可访问
└── 供应商详情
    ├── 官方来源
    ├── 方案与当前价格
    ├── 最近验证时间
    └── 数据说明

对比
├── 用途筛选：个人 / 团队 / 备份
├── 价格排序
├── 币种显示
└── 方案对比表或移动端卡片

方案详情
├── 方案名与供应商
├── 当前可验证价格
├── 来源和验证时间
├── 有效期 / 未公布说明
└── 前往官方页面
```

## 3. 首页方案

### 3.1 顶部导航

- 左侧：`Cloud Storage Deals` 品牌名 + 小型“Verified pricing”标识；
- 中间：`Deals`、`Providers`、`Compare`；
- 右侧：`How it works` 文本链接；
- 移动端：品牌名 + 汉堡菜单，点击区域至少 44×44px；
- 导航吸顶时使用不透明白色背景，不使用影响文本可读性的强玻璃效果。

### 3.2 Hero 区

主标题建议：

```text
Compare cloud storage prices from official pages.
```

副标题：

```text
Verified plans, clear sources, refreshed every 6 hours.
```

主操作：

- `Browse verified deals` → 进入价格列表；
- `Compare providers` → 进入对比页。

Hero 右侧显示一个“数据可信度卡片”：

```text
12 providers tracked
28 verified offers
Last checked 13:12 UTC
```

关键原则：更新时间必须靠近数据摘要，避免用户误以为价格是实时或永久有效。

### 3.3 快速筛选

使用三个明确的选择器，而不是让用户先阅读长表格：

- `Use case`：Personal / Team / Backup；
- `Storage size`：Up to 1 TB / 1–5 TB / 5 TB+；
- `Sort by`：Lowest verified price / Most capacity / Recently checked。

筛选器设计：

- 桌面端一行排列；
- 移动端纵向堆叠；
- 默认不隐藏数据，只改变排序和分组；
- 没有结果时显示清楚的空状态和“清除筛选”。

### 3.4 方案卡片

每张卡片保持 4 个层级：

1. 供应商名称 + 状态标签；
2. 方案名称；
3. 价格主视觉；
4. 来源、验证时间和操作按钮。

卡片示例结构：

```text
IDrive                         Verified
IDrive 2 TB
$11.99 USD                    current price
Verified Oct 15, 2026         Official source
[View details]                [Official page]
```

价格不可验证时：

```text
Price not publicly verifiable
Official page available · last checked Oct 15, 2026
[View provider]
```

禁止：

- 用 `$0`、`Free` 或估算值填充缺失价格；
- 使用“Best deal”但没有事实依据；
- 用红色表示价格低，用绿色表示价格高；
- 把划线原价当作当前价格。

## 4. 供应商页方案

### 总入口 `/providers/`

采用带筛选的列表，而不是单纯链接墙：

| 信息 | 说明 |
|---|---|
| Provider | 供应商名称和品牌标识 |
| Verified plans | 已验证方案数量 |
| Status | Verified / No public price / Unreachable |
| Official site | 官方站点链接 |

状态颜色仅用于辅助，必须同时显示文字：

- Verified：绿色文字 + `Verified`；
- No public price：琥珀色文字 + `No verified price`；
- Unreachable：红色文字 + `Source unavailable`。

### 供应商详情页

顶部提供：

- 面包屑；
- 供应商名称；
- 官方站点按钮；
- 最近检查时间；
- 数据状态说明。

方案区域按“当前价格 → 来源 → 说明”顺序排列。桌面端为表格，移动端自动变为卡片，不强迫用户横向滚动。

## 5. 对比页方案

### 桌面端

保留表格，但增加：

- 首列供应商固定；
- 价格列右对齐；
- 状态列显示文字标签；
- 鼠标悬停突出整行；
- 表头在长页面中吸顶。

### 移动端

不使用宽表格挤压内容，改成每家供应商一张对比卡：

```text
Jottacloud
Lowest verified price    $7.99 USD
Plans found              6
Status                   Verified
[View provider]
```

对比页顶部增加“比较口径”说明：

```text
Prices are shown exactly as published on official pages. Renewal prices and struck-through original prices are excluded.
```

## 6. 视觉设计系统

### 色彩

| 用途 | 色值 |
|---|---|
| 页面背景 | `#F7F7F4` |
| 卡片背景 | `#FFFFFF` |
| 主文字 | `#172033` |
| 次级文字 | `#566174` |
| 品牌紫 | `#6752D6` |
| 品牌紫浅色 | `#EEEAFE` |
| 验证绿色 | `#13795B` |
| 验证绿浅色 | `#E5F5EE` |
| 警告琥珀 | `#A66008` |
| 警告浅色 | `#FFF3D6` |
| 错误红 | `#B43D42` |
| 边框 | `#E4E7EC` |

说明：价格涨跌不采用金融市场红涨绿跌语义，因为本站不是行情产品；颜色只表达状态，不表达价格优劣。

### 字体

- 标题：`Newsreader`，体现编辑感和可信度；
- 正文：`Roboto` 或系统无衬线字体；
- 数字价格：正文字体的 600 权重，启用等宽数字；
- 页面语言为英文时，标题、H1 和正文保持英文一致，不混用中文标签。

### 尺寸与间距

- 内容最大宽度：`1120px`；
- 桌面边距：`32px`；
- 平板边距：`24px`；
- 手机边距：`16px`；
- 卡片圆角：`14px`；
- 控件圆角：`8px`；
- 主按钮高度：`44px`；
- 触摸目标最小尺寸：`44×44px`；
- 微交互：`150–250ms`，不使用大幅位移。

## 7. 组件规范

### 按钮

- 主按钮：品牌紫背景、白色文字；
- 官方来源按钮：白底、紫色边框；
- 不使用纯绿色作为主要 CTA，以避免与“Verified”状态混淆；
- hover 只改变颜色和边框，不改变布局尺寸；
- keyboard focus 使用 3px 高对比度 outline。

### 标签

标签必须包含文字，不依赖颜色单独表达含义：

```text
Verified
No verified price
Source unavailable
Expired
```

### 表格

- 用 `overflow-x-auto` 作为最后保护；
- 移动端优先提供卡片布局；
- 不把 5 列压缩成难以阅读的小字体；
- 表头和状态保持足够对比度。

### 空状态

必须告诉用户原因和下一步：

```text
No verified price found
The provider page was reachable, but a current public price could not be verified.
[View official page]
```

## 8. 用户习惯与可用性

1. **先找结果，再看解释**：首页首屏先显示可操作筛选和方案，不把方法论放在第一屏；
2. **价格必须带上下文**：价格旁边同时显示币种、方案和验证时间；
3. **来源一键可见**：每个价格都有官方来源链接；
4. **不隐藏异常**：无法验证、不可访问、已过期都使用明确状态；
5. **移动端优先**：375px 宽度下不出现横向页面滚动；
6. **不强制注册**：当前产品是公开信息站，不应设置登录墙；
7. **不使用误导 CTA**：按钮文案使用 `View details`、`Official page`、`Compare`；
8. **尊重辅助功能**：颜色不是唯一信息，所有状态同时有文字。

## 9. SEO 与界面协同

- 每页一个明确 H1；
- title 与 H1 说明真实页面内容，不批量使用相同标题；
- 结构化数据只标记页面可见内容；
- 方案详情页保留官方来源、验证时间和实际价格；
- sitemap 收录真实存在且 HTTP 200 的页面；
- 价格不确定时不要写入 `Offer.price`；
- 设计改版不改变现有 URL 结构；
- 新增 About / Methodology 页面并纳入导航和 sitemap。

## 10. 交付阶段

### Phase 1：先改信息层级

- 重做顶部导航；
- 重做首页 Hero 和数据摘要；
- 将价格列表改成方案卡片；
- 添加筛选器视觉结构；
- 保留现有 URL 和 SEO 标签。

### Phase 2：补齐状态与响应式

- 供应商状态标签统一；
- 供应商总入口移动端卡片化；
- 对比页移动端卡片化；
- 添加空状态、加载状态和键盘 focus 样式；
- 在 375 / 768 / 1024 / 1440px 检查布局。

### Phase 3：建立信任页面

- 增加 About / Methodology；
- 增加“如何验证价格”说明；
- 增加站点级 WebSite / Organization 数据；
- 通过 Search Console 和 Rich Results Test 检查上线结果。

## 11. 设计验收标准

- [ ] 首屏 10 秒内能理解站点用途；
- [ ] 首屏能进入方案列表或对比页；
- [ ] 每个价格显示币种、方案、供应商和验证时间；
- [ ] 价格缺失时不会显示估算数字；
- [ ] 移动端无横向页面滚动；
- [ ] 所有可点击控件具备 hover、focus 和 pointer 状态；
- [ ] 状态不依赖颜色单独表达；
- [ ] 保留现有 URL、canonical、sitemap 和 JSON-LD；
- [ ] 线上页面返回 HTTP 200；
- [ ] 通过 Google Rich Results Test 和 Search Console 检查。

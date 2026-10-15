ILANG
[TYPE:agents][PROJECT:cloud-storage-deals][LANG:zh][AUDIENCE:AI]

::STATE{@PROJECT, what:一个零服务器 零密钥 纯Python 的优惠细分垂直站, niche:cloud-storage, brand:Cloud Storage Deals}
::STATE{@SELF, role:maintainer, authority:proposal}
::STATE{@CONFIG, file:.ilang/site.ilang, role:唯一真源}

::MODULE{ARCH}
  siteilang.py  解析 .ilang/site.ilang  scraper.py 和 build.py 都从这里拿配置
  scraper.py    抓公开官方页 写 data/offers.json
  build.py      读 offers.json 加 templates 渲染到 site/
  templates/    base index provider deal compare
  .github/workflows/update.yml  每 6 小时跑一次 scraper 加 build 加 commit

::MODULE{ALLOWED}
  [A1] 改 .ilang/site.ilang 里的厂商清单 站点设置 字段 然后重跑 scraper.py 和 build.py
  [A2] 改 templates 的版式和文案
  [A3] 改 scraper.py 的抽取规则 让它更准 但只能更严 不能放宽到把估的价格写进去
  [A4] 加新的公开官方页作为数据源 前提是 robots.txt 放行
  [A5] 加语言子目录和 hreflang 走 SCALE 那节的顺序 一次只加一种语言

::MODULE{FORBIDDEN}
  [F1] 在 scraper.py 或 build.py 里硬编码厂商清单 一切从 .ilang/site.ilang 读
  [F2] 编价格 编优惠 编佣金 编 valid_until 抓不到就留空 留空是正确结果
  [F3] 用估的 或者用上一次的值 去填一个这轮没抓到的价格
  [F4] 抓 robots.txt 禁止的路径 绕反爬 抓登录后内容
  [F5] 品牌词竞价 cookie 注入 自买自推 买粉 刷量
  [F6] 把过期优惠留在页面上冒充有效 过期就标 expired 或下架

::MODULE{RULES}
  ::RULE{一条优惠等于一个真实方案⇒去重键 厂商 加 方案名 加 币种}
  ::RULE{只取当期售价⇒续费 原价 划线价 两年后 小字 一律不算}
  ::RULE{取不到方案名⇒整条丢弃 不许拿整段网页原文当标题}
  ::RULE{结构化数据不许造⇒抓不到 price 或 priceValidUntil 就不写那个字段}
  ::RULE{部署后⇒用 Cloudflare 给的**实际地址**回写 site.ilang 的 base_url 再重建}
  ::BOUNDARY{never:编数据 编价格 编佣金 刷量|scope:permanent}

::MODULE{CHECK}
  [C1] 改 site.ilang 里一家厂商 重跑 scraper 和 build 站上就该变 变不了说明配置成了摆设 重做
  [C2] sitemap 里的 deal 页数 要和页面上真实方案数对得上
  [C3] 随便挑一个详情页 把 JSON-LD 贴进谷歌富媒体测试工具 有报错先修再上线
  [C4] 打开线上 sitemap.xml 看每一条是不是自己的地址 Pages 项目名被占用时地址会带后缀

::FACT{key:runtime|value:纯Python标准库 无依赖 无密钥 无服务器}
::FACT{key:cost|value:GitHub Actions 公开仓库不限分钟数 Cloudflare Pages 免费额度}
::LESSON{id:why_ilang|type:meta|scope:project|conf:confirmed}
  配置写成 I-Lang 是因为它比 yaml 多表达一样东西 边界和禁止
  代码层不依赖任何 I-Lang 运行时 拿掉 site.ilang 换 yaml 站照样跑

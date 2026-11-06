# 配额同步插件卡片状态修复

宿主把“HTTP 透传未启用”显示为整个插件“已停用/未运行”，不适用于独立后台执行器。
本补丁读取插件自己的配置，准确区分“同步已启用”“已预约用卡”“演练模式”“业务已关闭”。
读取失败显示“状态待确认”；真实不兼容或宿主错误仍保留，不把开关状态冒充执行器健康。
后台插件隐藏无关的 OAuth 流量比例和透传启用按钮；其他插件不受影响。
每 15 秒只读刷新配置，组件卸载停止定时器，配置保存后立即更新展示。

## 源码与当前部署

- 完整源码补丁：patches/sub2api-background-plugin-status.patch，基于宿主 cf6d99ed2。
- 当前宿主把前端嵌入二进制，但原生支持 data/public 下同路径资源覆盖，无需重启服务。
- 只覆盖 PluginsView-MQqS5_Pd.js；patch_host_bundle.py 校验原资源 SHA256 和每个锚点次数。
- 宿主升级、资源名或哈希变化后应重新应用源码补丁并测试，不能盲目重用覆盖。
- 回滚时移走该单个覆盖文件，宿主即恢复内嵌版本；首次访问新版本可能需要 Ctrl+F5 清理旧的 immutable 资源缓存。

## 验证

- node --test host_ui/status.test.mjs：10 项状态语义测试。
- 宿主 Vitest：原插件页 6 项 + 新增业务状态 5 项通过。
- 线上编译资源在独立 Vue/jsdom 中渲染：7 项通过，普通插件和错误展示不变。
- vue-tsc --noEmit 通过；输出 JS 的 node --check 通过。
- 构建：python3 host_ui/patch_host_bundle.py 原始资源.js 覆盖资源.js。
- 这里不调用任何启用透传、重置配额或消费重置卡接口。

## 2026-11-06 完整前端发布（替代同名资源覆盖）

同名哈希资源覆盖已被确认不足以更新客户端 immutable 缓存；不再把强刷作为上线保证。
现在从宿主 cf6d99ed2 源码应用两个源码补丁（后台状态、资源路径与路由解耦）完整构建，
使用新路径 /frontend-quota-ui-20261106/，页面路由仍保持 /。

构建环境变量 VITE_ROUTER_BASE=/，Vite 参数 --base /frontend-quota-ui-20261106/ --outDir dist。
完整测试：插件页与编译资源 18 项、i18n 完整性 3 项、vue-tsc、Vite 生产构建通过。
部署脚本 deploy_frontend_release.py 保留后端 HTML 的站点设置注入与 CSP nonce，
只替换 HTML 入口中的静态资源 URL，并通过独立 Nginx 路径提供同一构建的所有资源。
旧资源保留，不打断已经打开的客户端；API 的压缩和条件请求头不变。
Nginx 配置先校验再平滑 reload，验证失败自动回滚，无需重启 Sub2API。
该发布限定宿主 cf6d99ed2；升级宿主时先迁移源码补丁并重新构建，不能无限延用旧前端。

验证：python3 -m unittest discover -s host_ui -p 'test_*.py'。

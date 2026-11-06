export const QUOTA_SYNC_KEY = "com.hzyhz.sub2api-quota-sync";

export function isQuotaSyncPlugin(plugin) {
  return plugin.plugin_key === QUOTA_SYNC_KEY;
}

// 只展示配置状态，不把“开关开启”冒充后台执行器健康。
export function quotaSyncStatus(plugin, config, locale = "zh") {
  if (!isQuotaSyncPlugin(plugin) || plugin.state !== "disabled" ||
      !plugin.compatibility?.compatible || plugin.last_error) return null;
  const zh = String(locale).startsWith("zh");
  const words = zh ? {
    pending: "状态待确认", off: "业务已关闭", sync: "同步已启用",
    dry: "演练模式", scheduled: "已预约用卡", mode: "后台任务模式",
    detail: "后台独立执行，无需启用 HTTP 透传。此处显示业务开关；执行状态请点击“测试”检查。"
  } : {
    pending: "Status unavailable", off: "Business tasks off", sync: "Sync enabled",
    dry: "Dry-run mode", scheduled: "Reset scheduled", mode: "Background tasks",
    detail: "Runs independently; HTTP passthrough is not required. This shows configuration; use Test to check the executor."
  };
  if (!config || typeof config.enabled !== "boolean") {
    return {label:words.pending,state:"starting",mode:words.mode,detail:words.detail};
  }
  const active = config.enabled || config.reset_card_schedule?.enabled === true;
  if (!active) return {label:words.off,state:"disabled",mode:words.mode,detail:words.detail};
  if (config.dry_run) return {label:words.dry,state:"starting",mode:words.mode,detail:words.detail};
  return {label:config.enabled?words.sync:words.scheduled,state:"enabled",mode:words.mode,detail:words.detail};
}

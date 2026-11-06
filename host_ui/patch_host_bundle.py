"""为已验证的宿主构建生成可回滚 UI 覆盖；不修改 API 状态或启用透传。"""
import argparse
import hashlib
from pathlib import Path

EXPECTED_SHA256 = "ac8849bc4310f783ad204ed31cf29f7d2f2dcfc17d7036810df824d22652bb1d"

def patch_bundle(source, helper):
    def change(old, new):
        nonlocal source
        if source.count(old) != 1:
            raise ValueError("宿主构建不匹配，拒绝套用覆盖: " + old[:80])
        source = source.replace(old,new)
    helper = helper.replace("export ", "")
    change("const xe=", helper + "\nconst xe=")
    change("const{t:a}=_e()", "const{t:a,locale:qsLocale}=_e()")
    change("let A=0;", """let A=0;
const qsConfigs=g({});let qsTimer;
function qsStatus(plugin){return quotaSyncStatus(plugin,qsConfigs.value[plugin.id],qsLocale?.value||"zh")}
async function qsRefresh(plugin){if(!isQuotaSyncPlugin(plugin))return;try{qsConfigs.value[plugin.id]=await f.plugins.getConfig(plugin.id)}catch{qsConfigs.value[plugin.id]=null}}
async function qsRefreshAll(){await Promise.all(U.value.filter(isQuotaSyncPlugin).map(qsRefresh))}
""")
    change("for(const e of U.value)z.value[e.id]=V(e)",
           "for(const e of U.value)z.value[e.id]=V(e);await qsRefreshAll()")
    change("ae(n.state)", "ae(qsStatus(n)?.state||n.state)")
    change('i(r(a)(`admin.plugins.${n.state}`))',
           'i(qsStatus(n)?.label||r(a)(`admin.plugins.${n.state}`))')
    change('i(n.runtime_healthy?r(a)("admin.plugins.healthy"):r(a)("admin.plugins.unhealthy"))',
           'i(qsStatus(n)?.mode||(n.runtime_healthy?r(a)("admin.plugins.healthy"):r(a)("admin.plugins.unhealthy")))')
    change('n.last_error?(u(),m("p",lt,i(n.last_error),1))',
           'qsStatus(n)?s("p",{class:"mt-3 text-xs text-gray-500"},i(qsStatus(n).detail),1):n.last_error?(u(),m("p",lt,i(n.last_error),1))')
    change('s("div",ct,[s("label",ut,', 'isQuotaSyncPlugin(n)?E("",!0):s("div",ct,[s("label",ut,')
    change(')):(u(),m("button",{key:1,type:"button",class:"btn btn-primary btn-sm"',
           ')):isQuotaSyncPlugin(n)?E("",!0):(u(),m("button",{key:1,type:"button",class:"btn btn-primary btn-sm"')
    change('I(t,{ok:!0,config:l}),c.showSuccess(a("common.saved"))',
           'qsConfigs.value[y.value.id]=l,I(t,{ok:!0,config:l}),c.showSuccess(a("common.saved"))')
    change('window.addEventListener("message",H),S()',
           'window.addEventListener("message",H),S(),qsTimer=setInterval(qsRefreshAll,15000)')
    change('window.removeEventListener("message",H),T()',
           'clearInterval(qsTimer),window.removeEventListener("message",H),T()')
    return source

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("input",type=Path)
    parser.add_argument("output",type=Path)
    args=parser.parse_args()
    raw=args.input.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=EXPECTED_SHA256:
        raise SystemExit("原始宿主资源 SHA256 不匹配；升级后必须重新构建/适配，不能静默套用")
    result=patch_bundle(raw.decode(),Path(__file__).with_name("quota-sync-status.mjs").read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(result,encoding="utf-8")
    print(hashlib.sha256(result.encode()).hexdigest())

if __name__=="__main__":
    main()

"""用独立资源版本发布宿主前端，保留后端设置注入、鉴权与旧客户端资源。"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "/frontend-quota-ui-20261106/"
RELEASE = Path("/opt/sub2api/deploy/frontend-releases/quota-ui-20261106")
SITE = Path("/etc/nginx/sites-available/sub2api_ai.conf")
MAP = Path("/etc/nginx/conf.d/sub2api-quota-ui-release.conf")
MARKER = "# quota-ui-release-20261106"
MAP_TEXT = """# 仅对 HTML 入口禁用上游压缩和旧内容协商；API 请求头保持原样。
map $http_accept $quota_ui_encoding {
    default $http_accept_encoding;
    "~*text/html" "";
}
map $http_accept $quota_ui_etag {
    default $http_if_none_match;
    "~*text/html" "";
}
map $http_accept $quota_ui_modified {
    default $http_if_modified_since;
    "~*text/html" "";
}
"""

def entry_assets(html, base):
    result = {}
    for value in re.findall(r'(?:src|href)="([^"]+)"', html):
        if not value.startswith(base+"assets/"):
            continue
        name = value.rsplit("/",1)[1]
        match = re.fullmatch(r"(.+)-[A-Za-z0-9_-]{8}\.(js|css)",name)
        if not match:
            raise ValueError("入口资源命名不符合预期: "+name)
        key = match.groups()
        if key in result:
            raise ValueError("入口资源类型重复")
        result[key]=value
    if ("index","js") not in result or ("index","css") not in result:
        raise ValueError("缺少入口 JS/CSS")
    return result

def render_site(original, old_html, new_html):
    if MARKER in original:
        raise ValueError("该前端版本已部署，拒绝重复修改")
    old=entry_assets(old_html,"/")
    new=entry_assets(new_html,BASE)
    if old.keys()!=new.keys():
        raise ValueError("新旧入口依赖结构变化，必须人工审阅")
    anchor="    location / {\n        limit_req zone=sub2api_api burst=60 nodelay;"
    if original.count(anchor)!=1:
        raise ValueError("站点代理结构变化，不能套用")
    rules = "".join(f"        sub_filter '{old[k]}' '{new[k]}';\n" for k in old)
    block=f"""    {MARKER}
    location ^~ {BASE} {{
        alias {RELEASE}/;
        expires 1y;
    }}

    location / {{
        proxy_set_header Accept-Encoding $quota_ui_encoding;
        proxy_set_header If-None-Match $quota_ui_etag;
        proxy_set_header If-Modified-Since $quota_ui_modified;
        sub_filter_once off;
{rules}        limit_req zone=sub2api_api burst=60 nodelay;"""
    return original.replace(anchor,block),new

def atomic_write(path, text):
    stat=path.stat() if path.exists() else None
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix=".quota-ui-")
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as f:
            f.write(text);f.flush();os.fsync(f.fileno())
        os.chmod(tmp,(stat.st_mode & 0o777) if stat else 0o644)
        if stat: os.chown(tmp,stat.st_uid,stat.st_gid)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def get(url, headers=None):
    with urllib.request.urlopen(urllib.request.Request(url,headers=headers or {}),timeout=30) as r:
        return r.status,r.headers,r.read()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--check",action="store_true")
    args=parser.parse_args()
    expected=subprocess.check_output(["git","-C","/opt/sub2api","rev-parse","--short=9","HEAD"],text=True).strip()
    assert expected=="cf6d99ed2", "宿主版本变化，停止部署"
    assert (RELEASE/"index.html").is_file()
    original=SITE.read_text()
    old_html=get("http://127.0.0.1:18080/",{"Accept":"text/html"})[2].decode()
    changed,assets=render_site(original,old_html,(RELEASE/"index.html").read_text())
    for asset in assets.values():
        assert (RELEASE/asset.removeprefix(BASE)).is_file()
    assert not MAP.exists(), "专用配置已存在"
    if args.check:
        print(json.dumps({"entry_assets":list(assets.values()),"configuration_rendered":True}));return
    backup=Path("/opt/sub2api/backups")/("frontend-release-"+datetime.now().strftime("%Y%m%d-%H%M%S"))
    backup.mkdir(mode=0o700)
    shutil.copy2(SITE,backup/"sub2api_ai.conf")
    (backup/"rollback.txt").write_text(f"恢复 {backup}/sub2api_ai.conf 到 {SITE}，移走 {MAP} 后 nginx -t && systemctl reload nginx。\n")
    try:
        atomic_write(MAP,MAP_TEXT)
        atomic_write(SITE,changed)
        subprocess.run(["nginx","-t"],check=True)
        subprocess.run(["systemctl","reload","nginx"],check=True)
        # reload 只发送信号；等待新 worker 接管，避免把短暂的旧响应误判为失败。
        for attempt in range(20):
            status,headers,body=get("https://ai.677001.xyz:8081/admin/plugins",
                                   {"Accept":"text/html","If-None-Match":'"old-cached-entry"'})
            html=body.decode()
            if status==200 and all(url in html for url in assets.values()):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("新版 HTML 入口未生效，自动回滚")
        assert "window.__APP_CONFIG__" in html, "后端设置注入丢失"
        nonce=re.search(r"'nonce-([^']+)'",headers.get("Content-Security-Policy","")).group(1)
        assert f'nonce="{nonce}"' in html, "CSP nonce 不匹配"
        for url in assets.values():
            code,_,content=get("https://ai.677001.xyz:8081"+url)
            assert code==200
            assert hashlib.sha256(content).digest()==hashlib.sha256((RELEASE/url.removeprefix(BASE)).read_bytes()).digest()
        assert get("http://127.0.0.1:18080/health")[0]==200
        print(json.dumps({"deployed":True,"backup":str(backup),"new_entry":assets[("index","js")],
                          "settings_injection_preserved":True,"csp_nonce_verified":True,
                          "asset_hashes_verified":True,"backend_restart":False}))
    except BaseException:
        shutil.copy2(backup/"sub2api_ai.conf",SITE)
        if MAP.exists(): MAP.unlink()
        subprocess.run(["nginx","-t"],check=True)
        subprocess.run(["systemctl","reload","nginx"],check=True)
        raise

if __name__=="__main__": main()

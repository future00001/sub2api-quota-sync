import unittest
from deploy_frontend_release import entry_assets,render_site,BASE
OLD='<script src="/assets/index-12345678.js"></script><link href="/assets/index-abcdefgh.css">'
NEW=f'<script src="{BASE}assets/index-87654321.js"></script><link href="{BASE}assets/index-hgfedcba.css">'
SITE='server {\n    location / {\n        limit_req zone=sub2api_api burst=60 nodelay;\n        proxy_pass http://127.0.0.1:18080;\n    }\n}\n'
class ReleaseTests(unittest.TestCase):
    def test_preserves_proxy_and_updates_entries(self):
        text,assets=render_site(SITE,OLD,NEW)
        self.assertIn('proxy_pass http://127.0.0.1:18080',text)
        self.assertIn('proxy_set_header If-None-Match $quota_ui_etag',text)
        self.assertIn("sub_filter '/assets/index-12345678.js'",text)
        self.assertEqual(assets[('index','js')],BASE+'assets/index-87654321.js')
    def test_mismatched_shape_refused(self):
        with self.assertRaises(ValueError): render_site(SITE,OLD,NEW+f'<script src="{BASE}assets/extra-12345678.js"></script>')
    def test_missing_js_refused(self):
        with self.assertRaises(ValueError): entry_assets('<link href="/assets/index-abcdefgh.css">','/')
    def test_duplicate_deployment_refused(self):
        with self.assertRaises(ValueError): render_site(SITE+'# quota-ui-release-20261106',OLD,NEW)
    def test_unexpected_proxy_refused(self):
        with self.assertRaises(ValueError): render_site('server {}',OLD,NEW)
if __name__=='__main__': unittest.main()

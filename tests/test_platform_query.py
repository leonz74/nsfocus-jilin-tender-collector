from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from tender_downloader.config import AppConfig
from tender_downloader.database import Database
from tender_downloader.http_client import HttpResult
from tender_downloader.models import Coverage, Notice, AttachmentRef
from tender_downloader.field_extract import extract_structured_fields
from tender_downloader.classify import infer_notice_industry
from tender_downloader.query import normalise_query, query_config
from tender_downloader.sources import source_display_name
from tender_downloader.sources.jilin_ggzy import JilinGGZYSource
from tender_downloader.sources.national_ggzy import NationalGGZYSource
from tender_downloader.webui.server import ConfigWebApp

CRITERIA = dict(start_date='2026-01-01', end_date='2026-09-11', city='', industry='金融', keyword='金融', notice_type='')


class QueryTests(unittest.TestCase):
    def test_industry_query_expands_synonyms_and_does_not_change_saved_config(self):
        data = {'start_date':'2025-01-01','end_date':'2025-12-31', 'sources':[
            {'type':'jilin_ggzy'}, {'type':'ccgp_search'}, {'type':'national_ggzy'}, {'type':'ccgp_archive'}]}
        original = copy.deepcopy(data)
        cfg = query_config(AppConfig(Path('/tmp/config.json'), data), CRITERIA, 'a'*32)
        self.assertEqual(data, original)
        self.assertEqual('', cfg.data['_query']['criteria']['keyword'])
        self.assertIn('银行', cfg.data['_query']['terms'])
        self.assertTrue(cfg.data['sources'][0]['_query_all_channels'])
        self.assertNotIn('_query_unsupported', cfg.data['sources'][3])
        self.assertEqual(20, cfg.data['sources'][3]['max_pages_per_category'])
        all_cfg = query_config(AppConfig(Path('/tmp/config.json'), data), {**CRITERIA,'industry':'','keyword':''}, 'b'*32)
        self.assertEqual([''], all_cfg.data['_query']['terms'], 'blank query must not silently become security-only')

    def test_invalid_query_rejected(self):
        for changes in ({'start_date':'2026-12-01'}, {'industry':'bogus'}, {'keyword':"银行' or 1=1"}):
            with self.assertRaises(ValueError):
                normalise_query({**CRITERIA, **changes})

    def test_jilin_filters_remote_dates_and_terms_and_follows_all_result_pages(self):
        class Client:
            requests = []
            def request(self, url, **kwargs):
                self.requests.append(url)
                page = len(self.requests)
                data = {'recordnum':3, 'datas':[{'title':'吉林银行项目', 'timestamp':'2026-06-01',
                    'docpuburl':f'https://www.jl.gov.cn/ggzy/{i}.html', 'tType':'工程建设', 'iType':'招标公告'}
                    for i in ([1,2] if page == 1 else [3])]}
                return HttpResult(url,200,{},json.dumps(data).encode())
        client=Client();source=JilinGGZYSource(client, {'_query_all_channels':True, '_query_terms':['银行','农信'], 'page_size':2,'max_pages_per_channel':10})
        coverage=Coverage(source=source.name)
        notices=list(source.iter_notices(date(2026,1,1),date(2026,9,11),coverage))
        self.assertEqual(3,len(notices));self.assertEqual(2,len(client.requests))
        expression=parse_qs(urlsplit(client.requests[0]).query)['searchword'][0]
        self.assertIn("gtitle='银行' or gtitle='农信'",expression)
        self.assertIn('2025-12-31 23:59:59',expression)
        self.assertIn('2026-09-12 00:00:00',expression)
        self.assertIn("tType='政府采购'",expression)
        self.assertFalse(coverage.truncated)

    def test_national_sends_every_keyword_to_platform(self):
        class Client:
            calls=[]
            def post_form(self,url,fields,headers=None):
                self.calls.append(dict(fields))
                return HttpResult(url,200,{},b'{"code":200,"data":{"pages":0,"records":[]}}')
        client=Client();source=NationalGGZYSource(client,{'_query_terms':['银行','信用社'],'window_days':366,'deal_types':['01'],'source_types':['1']})
        coverage=Coverage(source=source.name)
        self.assertEqual([],list(source.iter_notices(date(2026,1,1),date(2026,9,11),coverage)))
        self.assertEqual(['银行','信用社'],[c['FINDTXT'] for c in client.calls])
        self.assertEqual(2,coverage.pages)
        self.assertTrue(coverage.reached_start and coverage.reached_end)

    def test_new_query_excludes_old_sample_and_previous_query_and_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Database(Path(folder)/'db.sqlite')
            for ident,query_id,status in [('sample','','review'),('old','a'*32,'review'),('new','b'*32,'review'),('alias','b'*32,'alias')]:
                n=Notice('fixture',100,ident,'吉林银行项目','2026-06-01','https://www.jl.gov.cn/'+ident,metadata={'query_id':query_id})
                db.upsert_notice(n,status=status)
            result=db.list_notices(query_id='b'*32)
            self.assertEqual(['fixture:new'],[x['notice_id'] for x in result['items']])
            db.close()

    def test_header_only_exports_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'config.json';p.write_text(json.dumps({'start_date':'2026-01-01','end_date':'2026-09-11','database':'output/db.sqlite','output_dir':'output','sources':[{'type':'jilin_ggzy'}]}))
            app=ConfigWebApp(p)
            try:
                for ids in ([],['does-not-exist'],None):
                    with self.assertRaisesRegex(ValueError,'没有可导出'):
                        app.export_notices({},ids)
                    with self.assertRaisesRegex(ValueError,'没有可导出'):
                        app.export_download_links(ids)
                self.assertFalse((Path(folder)/'output/exports').exists())
            finally: app.close()

    def test_explicit_buyer_not_bank_vendor_controls_industry(self):
        n=Notice('fixture',100,'1','财政局代理银行项目','2026-06-01','https://www.jl.gov.cn/a')
        fields=extract_structured_fields(n,'采购人（甲方）:\n公主岭市财政局\n中标（成交）供应商（乙方）:\n吉林银行股份有限公司长春公主岭支行')
        self.assertEqual('公主岭市财政局',n.buyer)
        self.assertEqual('党政',infer_notice_industry(n,''))
        self.assertEqual('吉林银行股份有限公司长春公主岭支行',fields.winning_vendor)
        n.buyer=''
        fields=extract_structured_fields(n,'招标人名称:\n中国农业银行股份有限公司长春金融研修院\n中标人名称\n吉林亚泰建筑工程有限公司')
        self.assertEqual('金融',infer_notice_industry(n,''))
        self.assertEqual('吉林亚泰建筑工程有限公司',fields.winning_vendor)

    def test_source_name_resolution_never_requires_custom_login(self):
        self.assertEqual('自定义网站',source_display_name({'type':'custom_web'}))
        self.assertEqual('吉林省公共资源交易公共服务平台',source_display_name({'type':'jilin_ggzy'}))

    def test_ccgp_legacy_links_upgrade_only_exact_official_hosts(self):
        from tender_downloader.sources.ccgp_search import _parse_records
        page = b'<li><a href="http://www.ccgp.gov.cn/cggg/zygg/a.htm">one</a></li><li><a href="http://www.ccgp.gov.cn.evil.test/cggg/zygg/b.htm">two</a></li>'
        records = _parse_records(page, "text/html", "https://search.ccgp.gov.cn/")
        self.assertEqual("https://www.ccgp.gov.cn/cggg/zygg/a.htm", records[0].url)
        self.assertEqual("http://www.ccgp.gov.cn.evil.test/cggg/zygg/b.htm", records[1].url)

    def test_information_export_contains_one_row_per_download_url_without_downloading(self):
        import csv
        import io
        from tender_downloader.http_client import HttpClient
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'config.json'
            p.write_text(json.dumps({'start_date':'2026-01-01','end_date':'2026-09-11','database':'output/db.sqlite','output_dir':'output','sources':[{'type':'jilin_ggzy'}]}))
            db=Database(Path(folder)/'output/db.sqlite')
            first=Notice('fixture',100,'1','银行项目','2026-06-01','https://www.jl.gov.cn/one')
            missing=Notice('fixture',100,'2','无附件项目','2026-06-01','https://www.jl.gov.cn/two')
            db.upsert_notice(first);db.upsert_notice(missing)
            for name in ('招标文件.pdf','附件.xlsx'):
                db.record_artifact_ref(first,AttachmentRef('https://www.jl.gov.cn/files/'+name,original_name=name),status='available')
            db.close()
            app=ConfigWebApp(p)
            try:
                with patch.object(HttpClient,'request',side_effect=AssertionError('export must not download')):
                    rows=list(csv.DictReader(io.StringIO(app.export_notices({}).decode('utf-8-sig'))))
                self.assertEqual(3,len(rows))
                files=[r for r in rows if r['标讯ID']==first.identity]
                self.assertEqual(2,len(files))
                self.assertEqual({'https://www.jl.gov.cn/files/招标文件.pdf','https://www.jl.gov.cn/files/附件.xlsx'},{r['下载URL'] for r in files})
                empty=next(r for r in rows if r['标讯ID']==missing.identity)
                self.assertEqual('',empty['下载URL'])
                self.assertEqual('https://www.jl.gov.cn/two',empty['原公告URL'])
                self.assertEqual('', empty['下载URL'])
            finally: app.close()

if __name__=='__main__': unittest.main()

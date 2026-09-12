import csv
import io
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock, patch

from tender_downloader.database import Database
from tender_downloader.export import download_links_csv
from tender_downloader.field_extract import extract_structured_fields
from tender_downloader.htmlparse import parse_html_document
from tender_downloader.http_client import FetchError, HttpClient, HttpResult, SourceBlocked
from tender_downloader.models import AttachmentRef, Coverage, Notice
from tender_downloader.query_review import QueryReviewer
from tender_downloader.sources.ccgp_archive import CCGPArchiveSource


def notice(**kw):
    return Notice('test', 100, 'one', '某中学项目', '2026-09-09', 'https://example.gov.cn/notice/one.html', **kw)


class AuditFixTests(unittest.TestCase):
    def test_okcis_ai_mode_uses_full_text_search(self):
        from test_okcis import source, page, START, END
        s=source();s.config['_query_criteria']={'mode':'ai_recall'}
        state=page();state['contentMode']='1'
        s._wait_page=Mock(return_value=state);s._evaluate=Mock(return_value=True)
        self.assertEqual(1,len(list(s.iter_notices(START,END,Coverage(s.name)))))

    def test_okcis_default_does_not_stop_after_five_pages(self):
        from test_okcis import source, page, START, END
        s=source();s.config.pop('max_pages');s._evaluate=Mock(return_value=True)
        s._wait_page=Mock(side_effect=[page(i,i<6) for i in range(1,7)])
        coverage=Coverage(s.name);self.assertEqual(6,len(list(s.iter_notices(START,END,coverage))))
        self.assertFalse(coverage.truncated)

    def test_archive_navigation_cannot_become_the_first_notice(self):
        html = '<ul><li><a href="/">首页</a></li></ul><ul class="c_list_bid"><li><a href="/notice.htm" title="某中学采购">某中学采购</a>发布时间：<em>2026-09-09</em> 地域：<em>吉林</em> 采购人：<em>某中学</em></li></ul>'
        client = SimpleNamespace(request=lambda url: HttpResult(url,200,{},html.encode()))
        source = CCGPArchiveSource(client,{'scopes':['dfgg'],'categories':['gkzb'],'max_pages_per_category':1})
        rows = list(source.iter_notices(date(2026,9,1),date(2026,9,9),Coverage(source.name)))
        self.assertEqual(1,len(rows));self.assertEqual('某中学采购',rows[0].title)
        self.assertEqual('https://www.ccgp.gov.cn/notice.htm',rows[0].url)

    def test_placeholder_links_are_rejected_but_real_chinese_file_names_remain(self):
        html = '<a href="附件下载">附件下载</a><a href="%E9%99%84%E4%BB%B6%E4%B8%8B%E8%BD%BD">附件</a><a href="附件下载.pdf">附件</a><a href="/file?id=3">下载</a>'
        refs = parse_html_document(html.encode(), notice().url).attachments
        self.assertEqual(['https://example.gov.cn/notice/附件下载.pdf', 'https://example.gov.cn/file?id=3'], [r.url for r in refs])

    def test_legacy_bad_reference_exports_original_notice_and_reason_without_download_url(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d)/'test.sqlite3');n=notice();db.upsert_notice(n)
            db.record_artifact_ref(n, AttachmentRef('https://example.gov.cn/附件下载', '附件下载', '附件下载'), status='discovered')
            row = next(csv.DictReader(io.StringIO(download_links_csv(db).decode('utf-8-sig'))))
            self.assertEqual('', row['下载URL']);self.assertEqual(n.url, row['公告URL'])
            self.assertIn('无效链接', row['失败或待办原因']);self.assertEqual([], db.attachment_refs_for_notice(n.identity))
            db.close()

    def test_blank_code_skips_next_field_and_finds_a_later_real_code(self):
        self.assertEqual('', extract_structured_fields(notice(), '招标编号\n更新时间\n2026-09-09').project_code)
        self.assertEqual('JL-2026-123', extract_structured_fields(notice(), '招标编号\n更新时间\n项目编号：JL-2026-123\n采购单位：某中学').project_code)
        self.assertEqual('JLSZC2026001', extract_structured_fields(notice(), '项目编号\nJLSZC2026001\n项目名称\n某中学').project_code)

    def test_unreadable_archive_channel_does_not_skip_healthy_channels(self):
        good='<ul class="c_list_bid"><li><a href="/202609/t20260909_1.htm" title="中学">中学</a>发布时间：<em>2026-09-09</em> 地域：<em>吉林</em> 采购人：<em>中学</em></li></ul>'
        def request(url):
            return HttpResult(url,200,{},(good if '/dfgg/' in url else '<html>unexpected</html>').encode())
        c=SimpleNamespace(request=request)
        s=CCGPArchiveSource(c,{'scopes':['zygg','dfgg'],'categories':['gkzb'],'max_pages_per_category':1})
        coverage=Coverage(s.name);rows=list(s.iter_notices(date(2026,9,1),date(2026,9,9),coverage))
        self.assertEqual(1,len(rows));self.assertTrue(coverage.truncated)
        self.assertIn('其余栏目继续采集', coverage.message)

    def test_archive_retry_preserves_a_real_access_block(self):
        client=SimpleNamespace(request=Mock(side_effect=[HttpResult('u',200,{},b'bad'),SourceBlocked('HTTP 429')]))
        s=CCGPArchiveSource(client,{'scopes':['zygg','dfgg'],'categories':['gkzb']})
        with self.assertRaises(SourceBlocked):list(s.iter_notices(date(2026,9,1),date(2026,9,9),Coverage(s.name)))
        self.assertEqual(2,client.request.call_count)

    def test_failed_cached_curl_uses_verified_python_transport(self):
        client=HttpClient(user_agent='test',delay_seconds=0,max_retries=0)
        url='https://example.gov.cn/a';client._curl_domains.add(client._domain_key(url))
        response=Mock();response.status=200;response.headers={'Content-Type':'text/html'};response.geturl.return_value=url
        client._opener=MagicMock();client._opener.open.return_value.__enter__.return_value=response
        try:
            with patch.object(client,'_validate_url'),patch.object(client,'_read_response',return_value=b'ok'),patch.object(client,'_curl_request',side_effect=FetchError('curl TLS failed')):
                result=client.request(url)
            self.assertEqual(b'ok',result.body);self.assertNotIn(client._domain_key(url),client._curl_domains)
        finally:client.close()

    def test_ai_recovers_after_cooldown_instead_of_disabling_the_whole_run(self):
        now=[0.0];n=notice(origin_verified=True,body_text='某中学项目采购')
        good={'checks':{'keyword':{'verdict':'match','confidence':.99,'evidence':['某中学']}},'reason':'匹配'}
        model=SimpleNamespace(model='fixture',_call_model=Mock(side_effect=[FetchError('temporary')]*3+[good]))
        reviewer=QueryReviewer(model,{'keyword':'中学'},clock=lambda:now[0])
        for _ in range(3):self.assertEqual('error',reviewer.review(n)['status'])
        self.assertEqual('not_run',reviewer.review(n)['status']);self.assertEqual(3,model._call_model.call_count)
        now[0]=31;self.assertEqual('match',reviewer.review(n)['status']);self.assertEqual(0,reviewer.failures)

from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tender_downloader.http_client import FetchError, SourceBlocked, HttpResult
from tender_downloader.models import Coverage, Notice, RawDocument
from tender_downloader.sources import build_sources
from tender_downloader.sources.okcis import OKCISSource, SEARCH
from tender_downloader.sources.ccgp_archive import CCGPArchiveSource

START, END = date(2026, 1, 1), date(2026, 9, 11)


def source():
    client = SimpleNamespace(timeout_seconds=10, allow_private_hosts=False)
    client.fork_session = lambda: client
    config = {"type":"custom_web", "id":"okcis", "adapter":"okcis", "name":"导航网",
              "start_urls":[SEARCH], "auth":{"mode":"browser"}, "_query_terms":["网络安全"],
              "max_pages":2, "verification_wait_seconds":10}
    result = OKCISSource(client, config)
    clock = [0]
    def sleep(seconds): clock[0] += seconds
    result._transport = SimpleNamespace(_clock=lambda:clock[0], _sleeper=sleep,
                                        handles=lambda u:u.startswith("https://www.okcis.cn/"))
    result._prepared = True
    return result


def page(number=1, next_page=False):
    return {"url":SEARCH,"keyword":"网络安全","contentMode":"2", "dates":[str(START), str(END)],
            "result":True, "next":next_page, "rows":[{"url":f"https://www.okcis.cn/20260910-n2-{number}.html",
            "title":"吉林大学网络安全采购", "date":"2026-09-10", "type":"招标公告", "region":"吉林-长春"}]}


class OKCISTests(unittest.TestCase):
    def test_source_factory_uses_specific_adapter(self):
        s = source()
        self.assertIsInstance(build_sources(s.client, [s.config])[0], OKCISSource)

    def test_pagination_and_duplicate_suppression(self):
        s=source();s._evaluate=Mock(return_value=True)
        s._wait_page=Mock(side_effect=[page(1,True),page(2)])
        coverage=Coverage(s.name)
        notices=list(s.iter_notices(START, END, coverage))
        self.assertEqual(2,len(notices));self.assertEqual(2,coverage.pages)
        self.assertEqual("吉林-长春",notices[0].region)
        self.assertTrue(coverage.reached_start and coverage.reached_end)
        self.assertFalse(coverage.truncated)

    def test_page_cap_reports_incomplete_and_keeps_rows(self):
        s=source();s.config['max_pages']=1;s._evaluate=Mock(return_value=True)
        s._wait_page=Mock(return_value=page(1,True));coverage=Coverage(s.name)
        self.assertEqual(1,len(list(s.iter_notices(START, END, coverage))))
        self.assertTrue(coverage.truncated);self.assertIn("分页上限",coverage.message)

    def test_empty_keyword_does_not_use_subscription_or_old_results(self):
        s=source();s.config['_query_terms']=[''];s._evaluate=Mock();coverage=Coverage(s.name)
        self.assertEqual([],list(s.iter_notices(START,END,coverage)))
        self.assertTrue(coverage.truncated);s._evaluate.assert_not_called()

    def test_wrong_query_confirmation_fails_instead_of_publishing_stale_results(self):
        s=source();s._evaluate=Mock(return_value=True)
        wrong=page();wrong['keyword']='其他查询';s._wait_page=Mock(return_value=wrong)
        with self.assertRaisesRegex(FetchError,"当前关键词/日期"):
            list(s.iter_notices(START,END,Coverage(s.name)))

    def test_human_challenge_only_polls_then_resumes(self):
        s=source();events=[];s.set_progress_callback(lambda *e:events.append(e))
        # One solve attempt per challenge observation; three failures, then the
        # existing human wait: spinner-only UI action appears once, no reload.
        failed={'solved':False,'reason':'check_failed'}
        s._evaluate=Mock(side_effect=[{'challenge':True},failed,True,
            {'challenge':True},failed,{'challenge':True},failed,page()])
        state=s._wait_page(SEARCH,results=True)
        self.assertEqual(1,len(state['rows']))
        self.assertEqual(['waiting_verification','running'],[e[0] for e in events])
        self.assertNotIn('reload',str(s._evaluate.call_args_list))

    def test_challenge_auto_solved_resumes_search_without_human(self):
        s=source();events=[];s.set_progress_callback(lambda *e:events.append(e))
        s._evaluate=Mock(side_effect=[{'challenge':True},{'solved':True,'guess':147},True,page()])
        state=s._wait_page(SEARCH,results=True,retry_action='NORMAL_SEARCH')
        self.assertEqual(1,len(state['rows']))
        self.assertEqual(['running'],[e[0] for e in events])
        self.assertEqual(1,sum(c.args[1]=='NORMAL_SEARCH' for c in s._evaluate.call_args_list))
        self.assertEqual(True,s._evaluate.call_args_list[1].kwargs.get('await_promise'))

    def test_challenge_auto_solved_detail_page_reloads(self):
        s=source()
        detail='https://www.okcis.cn/20260910-n2-1.html'
        s._evaluate=Mock(side_effect=[{'challenge':True},{'solved':True,'guess':80},True,
                                      {'url':detail,'html':'<div id="copyquyu">正文</div>','attachments':[]}])
        state=s._wait_page(detail)
        self.assertIn('正文',state['html'])
        self.assertIn('location.reload',str(s._evaluate.call_args_list))

    def test_verification_redirect_resubmits_same_search_once(self):
        s=source()
        failed={'solved':False,'reason':'no_hole'}
        s._evaluate=Mock(side_effect=[{'challenge':True},failed,True,
            {'challenge':True},failed,{'challenge':True},failed,
            {'returnedToSearch':True},True,page()])
        s._wait_page(SEARCH,results=True,retry_action='NORMAL_SEARCH')
        self.assertEqual(1,sum(c.args[1]=='NORMAL_SEARCH' for c in s._evaluate.call_args_list))

    def test_unfinished_challenge_times_out_without_zero_result(self):
        s=source();s._evaluate=Mock(return_value={'challenge':True})
        with self.assertRaisesRegex(SourceBlocked,'等待超时'):
            s._wait_page(SEARCH,results=True)

    def test_blank_result_is_error_and_unchanged_next_page_is_error(self):
        for state, previous in [({},()),(page(),tuple(r['url'] for r in page()['rows']))]:
            s=source();s._evaluate=Mock(return_value=state)
            with self.assertRaisesRegex(FetchError,'不能判定为零条'):
                s._wait_page(SEARCH,results=True,previous=previous)

    def test_download_entry_is_browser_link_and_restricted_file_not_faked(self):
        s=source();n=Notice(s.name,40,'1','公告','2026-09-10','https://www.okcis.cn/20260910-n2-1.html')
        s._attachments[n.url]=[{'url':'https://www.okcis.cn/php/savefuj.php?fj=a.pdf','label':'附件.pdf','restricted':False},
                               {'url':'','label':'会员文件','restricted':True}]
        refs=s.discover_artifacts(n,RawDocument(b"<a onclick=\"savefujian('2026/09/10/a.pdf','1','0')\">download</a>",n.url,{}))
        self.assertEqual(1,len(refs));self.assertEqual('public_browser',refs[0].access)


    def test_archive_404_boundary_is_retention_not_failure(self):
        good = ('<div class="c_list_bid"><ul class="c_list_bid">'
                '<li><a href="/notice1.htm" title="项目">项目</a>'
                '发布时间：<em>2026-09-01</em> 地域：<em>吉林长春</em> 采购人：<em>某单位</em></li></ul></div>')
        class Client:
            timeout_seconds = 5
            sleeper = staticmethod(lambda s: None)
            def __init__(self):
                self.calls = 0
            def request(self, url):
                self.calls += 1
                if url.endswith(('index.htm', 'index_1.htm')):
                    return HttpResult(url, 200, {'content-type': 'text/html'}, good.encode())
                raise FetchError(f"HTTP 404: {url}")
        source = CCGPArchiveSource(Client(), {'scopes': ['zygg'], 'categories': ['gkzb'], 'max_pages_per_category': 10})
        coverage = Coverage(source.name)
        notices = list(source.iter_notices(START, END, coverage))
        self.assertGreaterEqual(len(notices), 1)
        self.assertTrue(coverage.truncated)
        self.assertFalse(coverage.reached_start)
        self.assertIn('归档仅保留', coverage.message or '')

    def test_archive_blocked_markup_is_not_an_empty_list(self):
        c=SimpleNamespace(request=lambda *a:HttpResult(a[0],200,{},b'<html>unexpected</html>'))
        s=CCGPArchiveSource(c,{'scopes':['dfgg'],'categories':['gkzb']})
        with self.assertRaisesRegex(FetchError,'不能判定为零条'):
            list(s.iter_notices(START,END,Coverage(s.name)))

    def test_pagination_reset_after_auto_solve_stops_instead_of_looping(self):
        s=source();s.config['max_pages']=5
        s._evaluate=Mock(return_value=True)
        # Page 1 collected, then an auto-solved challenge re-runs the search and
        # the source keeps landing on page one: stop after the second reset.
        s._wait_page=Mock(side_effect=[page(1,True),page(1,True),page(1,True)])
        coverage=Coverage(s.name)
        self.assertEqual(1,len(list(s.iter_notices(START,END,coverage))))
        self.assertTrue(coverage.truncated);self.assertIn('回到第一页',coverage.message)


    def test_silent_submit_failure_replays_search_once(self):
        s=source()
        # First wait times out with no challenge; the replay then succeeds.
        s._evaluate=Mock(side_effect=[True, page()])
        s._transport._clock=lambda: __import__('time').monotonic()
        def fake_sleep(sec): pass
        s._transport._sleeper=fake_sleep
        # force first _wait_page_once to hit its deadline immediately
        orig=s._wait_page_once
        calls={'n':0}
        def once(url, **kw):
            calls['n']+=1
            if calls['n']==1:
                raise FetchError('导航网没有返回有效结果或翻页无变化，不能判定为零条公告')
            return orig(url, **kw)
        s._wait_page_once=once
        state=s._wait_page(SEARCH,results=True,retry_action='NORMAL_SEARCH')
        self.assertEqual(1,len(state['rows']))
        self.assertEqual(1,sum(c.args[1]=='NORMAL_SEARCH' for c in s._evaluate.call_args_list))


    def test_title_mode_empty_falls_back_to_full_text_mode(self):
        s=source()
        s._evaluate=Mock(return_value=True)
        # First wait fails (title mode starved), full-text fallback succeeds.
        fulltext=page(); fulltext['contentMode']='1'
        s._wait_page=Mock(side_effect=[FetchError('导航网没有返回有效结果或翻页无变化，不能判定为零条公告'), fulltext])
        coverage=Coverage(s.name)
        notices=list(s.iter_notices(START,END,coverage))
        self.assertEqual(1,len(notices))
        self.assertEqual(2,s._wait_page.call_count)

    def test_embedded_js_snippets_are_valid_syntax(self):
        import shutil, subprocess, tempfile, os
        from tender_downloader.sources import okcis as module
        node=shutil.which('node')
        if not node:
            self.skipTest('node not available')
        for name in ('SNAPSHOT','CAPTCHA_SOLVE','NEXT_ACTION','DETAIL_FETCH'):
            code=getattr(module,name)
            with tempfile.NamedTemporaryFile('w',suffix='.js',delete=False) as fh:
                fh.write(code)
                path=fh.name
            try:
                result=subprocess.run([node,'--check',path],capture_output=True,text=True)
                self.assertEqual(0,result.returncode,f'{name}: {result.stderr}')
            finally:
                os.unlink(path)


if __name__ == '__main__': unittest.main()

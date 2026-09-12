from pathlib import Path
import copy
import tempfile
import unittest

from tender_downloader.classify import infer_notice_industry
from tender_downloader.config import AppConfig, sample_config
from tender_downloader.database import Database
from tender_downloader.field_extract import extract_structured_fields
from tender_downloader.models import Notice
from tender_downloader.regions import infer_notice_city


class BeginnerWorkflowTests(unittest.TestCase):
    def notice(self, title="长春市某代理机构关于东丰县第三中学采购项目公告"):
        return Notice(source="fixture", authority_rank=100, external_id="1", title=title,
                      published_at="2026-09-10", url="https://www.jl.gov.cn/notice/1", region="吉林省")

    def test_city_uses_project_district_not_procuring_agency_city(self):
        self.assertEqual("辽源", infer_notice_city(self.notice()))
        self.assertEqual("长春", infer_notice_city(self.notice("双阳区福利服务中心改造公告")))
        self.assertEqual("吉林省", infer_notice_city(self.notice("吉林省某单位采购公告")))
        self.assertEqual("延边", infer_notice_city(self.notice("汪清县采购项目")))

    def test_purchaser_name_and_address_are_separate_from_agent(self):
        notice = self.notice()
        fields = extract_structured_fields(notice,
            "1.采购人信息\n名 称：\n东丰县第三中学\n地 址：\n东丰县西城区\n联系方式：15004378898\n"
            "2.采购代理机构信息\n名称：长春市某代理公司\n地址：长春市南关区")
        self.assertEqual("东丰县第三中学", notice.buyer)
        self.assertEqual("辽源", fields.city)
        self.assertEqual("15004378898", fields.buyer_phone)
        self.assertEqual("教育", infer_notice_industry(notice,"人民政府 财政局"))

    def test_industry_ignores_agency_names_and_procurement_boilerplate(self):
        notice = self.notice("教育咨询有限公司关于东升村道路改造项目")
        self.assertEqual("其他行业", infer_notice_industry(notice,
            "监狱企业及残疾人福利性单位视同小型、微型企业。网站导航：省公安厅"))
        notice.buyer = "寿山镇人民政府"
        self.assertEqual("党政", infer_notice_industry(notice, "采购学校设备"))
        self.assertEqual("医疗卫生", infer_notice_industry(self.notice(
            "梅河口市紧密型县域医共体共享中心建设项目"), ""))

    def test_uncertain_location_and_missing_contacts_are_not_invented(self):
        notice = self.notice("某单位网络安全服务采购公告")
        fields = extract_structured_fields(notice,"采购代理机构信息\n名称：长春市代理有限公司\n地址：长春市")
        self.assertEqual("", notice.buyer)
        self.assertEqual("吉林省", fields.city)
        self.assertEqual("", fields.buyer_contact)

    def test_sample_does_not_rewrite_scope_or_require_ai_credentials(self):
        data={"start_date":"2026-09-05","end_date":"2026-09-11","output_dir":"output",
              "database":"output/state.sqlite3", "ai":{"enabled":True},
              "delivery":{"mode":"automatic"}, "sources":[
                  {"type":"jilin_ggzy","enabled":False,"page_size":100},
                  {"type":"custom_web","id":"private","auth":{"mode":"browser"}}]}
        original=copy.deepcopy(data)
        config=AppConfig(Path("/fixture/config.json"),data)
        sample=sample_config(config)
        self.assertEqual(original,data)
        self.assertEqual(config.path,sample.path)
        self.assertFalse(sample.data["ai"]["enabled"])
        self.assertEqual("on_demand",sample.data["delivery"]["mode"])
        self.assertEqual(1,len(sample.data["sources"]))
        self.assertEqual(10,sample.data["sources"][0]["page_size"])
        self.assertEqual(1,sample.data["sources"][0]["max_pages_per_channel"])

    def test_unknown_filter_can_be_exported_by_the_server(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Database(Path(directory)/"state.sqlite3")
            db.upsert_notice(self.notice())
            self.assertEqual(1,db.list_notices(industry="__unknown__")["total"])
            self.assertEqual(0,db.list_notices(industry="教育")["total"])
            db.close()

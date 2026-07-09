from datetime import date

from shipwatch.config import load_settings
from shipwatch.extract import LLMExtractor, RuleExtractor


def test_rule_extractor_finds_project():
    extractor = RuleExtractor(load_settings("config.yaml"))
    result = extractor.extract(
        title="沪东中华一艘LNG船开工",
        content="5月8日，沪东中华为远洋航运建造2艘17.4万立方米LNG船，首制船正式开工。",
        yard_hint="沪东中华",
        published_at=date(2026, 5, 8),
    )
    assert result.relevant
    assert result.yard == "沪东中华"
    assert result.ship_count == 2
    assert result.current_progress == "开工"


def test_rule_extractor_excludes_warship():
    extractor = RuleExtractor(load_settings("config.yaml"))
    result = extractor.extract(
        title="某驱逐舰交付",
        content="新型驱逐舰交付入列。",
        yard_hint="江南造船",
        published_at=date(2026, 5, 8),
    )
    assert not result.relevant


def test_rule_extractor_does_not_treat_company_name_as_ship_type():
    extractor = RuleExtractor(load_settings("config.yaml"))
    result = extractor.extract(
        title="厦船重工高端油化船正式交付",
        content="厦船重工为德国GTS公司建造的41000DWT高端油化船正式交付。",
        yard_hint="厦门船舶重工",
        published_at=date(2026, 1, 1),
    )
    assert result.ship_type
    assert result.ship_type != "厦船"
    assert "油化船" in result.ship_type


def test_llm_extractor_parses_review_status():
    result = LLMExtractor._from_dict(
        {
            "relevant": False,
            "yard": None,
            "owner_project": None,
            "ship_type": None,
            "ship_count": None,
            "series_identifier": None,
            "current_progress": None,
            "start_date": None,
            "completion_date": None,
            "confidence": 0.92,
            "review_status": "无关",
            "review_reason": "党建新闻，不是新船项目",
            "milestones": [],
        }
    )

    assert result.review_status == "无关"
    assert result.review_reason == "党建新闻，不是新船项目"

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


def test_rule_extractor_excludes_delivery_article():
    extractor = RuleExtractor(load_settings("config.yaml"))
    result = extractor.extract(
        title="厦船重工高端油化船正式交付",
        content="厦船重工为德国GTS公司建造的41000DWT高端油化船正式交付。",
        yard_hint="厦门船舶重工",
        published_at=date(2026, 1, 1),
    )
    assert not result.relevant


def test_rule_extractor_excludes_delivery_and_sea_trial_articles():
    extractor = RuleExtractor(load_settings("config.yaml"))
    result = extractor.extract(
        title="第五代 LNG 船试航出坞接连告捷",
        content="沪东中华完成 LNG 船试航与出坞节点，建造团队保障按期交付。",
        yard_hint="沪东中华",
        published_at=date(2026, 5, 8),
    )
    assert not result.relevant
    assert result.review_status == "无关"


def test_rule_extractor_excludes_historical_start_mentioned_in_background():
    extractor = RuleExtractor(load_settings("config.yaml"))
    result = extractor.extract(
        title="总装团队精益创新提升 LNG 船建造效率",
        content="沪东中华总装团队开展精益改善。该船此前已顺利开工，目前正在进行后续建造。",
        yard_hint="沪东中华",
        published_at=date(2026, 5, 8),
    )
    assert not result.relevant


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

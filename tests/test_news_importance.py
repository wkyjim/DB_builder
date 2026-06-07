from datetime import datetime, timezone

from db_builder.news_importance import score_event_priority, score_news_importance, sort_classification_queue


def score(title: str, summary: str = "") -> float:
    return score_news_importance(title, summary)["importance_score"]


def test_cpi_headline_scores_high():
    assert score("CPI inflation report sends Treasury yields higher") >= 35


def test_fed_speech_scores_high():
    assert score("Fed Chair Powell says rate cut timing depends on inflation") >= 35


def test_iran_hormuz_headline_scores_high():
    assert score("Iran warns of Strait of Hormuz disruption after missile strikes") >= 35


def test_tariff_headline_scores_high():
    assert score("New tariffs and export controls hit China chip supply chain") >= 35


def test_earnings_guidance_headline_scores_high():
    assert score("Company beats earnings estimates and raises revenue guidance") >= 30


def test_ma_headline_scores_high():
    assert score("Chipmaker announces acquisition in $12 billion takeover") >= 30


def test_generic_stocks_rise_scores_lower():
    assert score("Stocks rise as market today looks calm") < score("CPI inflation report sends yields higher")


def test_should_you_buy_scores_lower():
    assert score("Should you buy these best stocks before they rise?") < 10


def test_queue_sorting_prioritizes_importance_over_recency():
    old_important = {
        "title": "Fed Powell warns rate hike possible after CPI inflation report",
        "summary": "",
        "source_priority": 80,
        "published_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
        "fetched_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
    }
    recent_generic = {
        "title": "Stocks rise: what to watch in market today",
        "summary": "",
        "source_priority": 100,
        "published_at": datetime(2026, 6, 7, tzinfo=timezone.utc),
        "fetched_at": datetime(2026, 6, 7, tzinfo=timezone.utc),
    }

    queue = sort_classification_queue([recent_generic, old_important])

    assert queue[0]["title"] == old_important["title"]


def test_event_priority_detects_top_tier_events():
    assert score_event_priority("FOMC decision keeps rates unchanged")["event_type_priority"] == 100
    assert score_event_priority("CPI report hotter than expected")["event_type_priority"] == 100
    assert score_event_priority("New tariffs announced on China imports")["event_type_priority"] == 95
    assert score_event_priority("Iran war escalates after missile strikes")["event_type_priority"] == 95


def test_event_priority_detects_mid_and_low_priority_events():
    assert score_event_priority("Company reports earnings and revenue beat")["event_type_priority"] == 90
    assert score_event_priority("Chipmaker announces acquisition")["event_type_priority"] == 90
    assert score_event_priority("Analyst says stock has upside")["event_type_priority"] == 40
    assert score_event_priority("Stocks rise in market today")["event_type_priority"] == 20


def test_classification_priority_uses_event_and_source_priority():
    fomc = {
        "title": "FOMC decision keeps rates unchanged after CPI report",
        "summary": "",
        "source_priority": 90,
        "published_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
        "fetched_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
    }
    recap = {
        "title": "Stocks rise in market today after a strong week",
        "summary": "",
        "source_priority": 90,
        "published_at": datetime(2026, 6, 7, tzinfo=timezone.utc),
        "fetched_at": datetime(2026, 6, 7, tzinfo=timezone.utc),
    }

    queue = sort_classification_queue([recap, fomc])

    assert queue[0]["title"] == fomc["title"]
    assert queue[0]["classification_priority"] > queue[1]["classification_priority"]
